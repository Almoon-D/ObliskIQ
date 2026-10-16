"""The pre-trade gate: the only issuer of a :class:`~obliskiq.domain.orders.PretradeClearance`.

No placement or modify reaches the broker without a clearance (I1, SESSION_PROMPTS Session 4
item 4). The gate clears an intent for one write only when every check passes, and otherwise
refuses it with a reason code per failure (:class:`Reason`). It works from the stored intent,
never from the object a caller holds.

**What it checks.** Three stages, each reporting every failure it finds:

1. *Local*, before the broker is touched: the intent (stored, in a state that may be written,
   unexpired, the action's terms); no currency conversion yet (Session 5 funds by conversion);
   the paper guard (profile paper ⇒ a paper-prefix account); the mode (the intent's, permitting
   writes under this profile); the kill switch; the instrument described; prohibited products;
   buy-side eligibility through the jurisdiction's :class:`EligibilityPolicy` (the mandatory
   close for an option on an ineligible ETP); approval; the calendars' act-now view at this
   instant (``Calendars.tradability``; an option also inside ``common_windows`` with its
   underlying's venue, and date-critical inside its mandatory-close window).
2. *Broker reads*: positions, live orders (a complete snapshot), trades, the quote policy and the
   collar (a protective stop: its protective spec instead), and, for an order that adds risk,
   the ledger and a whatif.
3. *Store*, in one transaction that also records the clearance, so two processes see each
   other: the holdings registry (``unmanaged`` never traded, a held conid without a sleeve
   refused), no short (I2, ``execution/exposure.py``), and, for an order that adds risk, the
   cash in its currency against every open commitment and the floor, no borrowing by the
   whatif's own cost, and the caps per order, per 24 hours and in count.

**Class by effect (I3).** A sell of a managed long position, within it, reduces risk; every buy
and every conversion adds risk. For an order that reduces risk the gate waives exactly the kill
switch, the notional caps, the order count, buy-side eligibility, the cash and no-margin check,
and approval; every other check applies, and a sell stop-limit is checked against its
protective spec instead of the collar. A refused option close for want of a real-time quote
alerts the owner: an option left to expiry may be exercised.

**The clearance** carries the effect, the quotes it was checked against and the collar's sigma
(the reply policy decides IBKR's no-market-data warning from them), the order's notional, and
expires with the earliest quote it rests on (and never after the intent).

:meth:`PretradeGate.check` runs the same checks without issuing anything, counting named orders
at an assumed size: an exit that needs its protective stop resized passes its other checks
before the stop is touched (I5, ``execution/exits.py``).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from ..calendars.service import Calendars
from ..config.schema import ObliskConfig
from ..config.schema_execution import NavPriced
from ..domain.enums import AssetClass
from ..domain.instrument import TradedInstrument
from ..domain.orders import (
    UNMANAGED,
    ClearanceAction,
    Fill,
    IntentState,
    OrderIntent,
    OrderSide,
    OrderTerms,
    PretradeClearance,
    QuoteEvidence,
    RiskEffect,
)
from ..errors import CalendarError, ObliskError
from ..ibkr.errors import AccountGuardError, IbkrError
from ..ibkr.guard import AccountGuard
from ..ibkr.order_decode import LiveOrders, WhatIf
from ..ibkr.orders import is_live
from ..jurisdiction.eligibility import EligibilityPolicy
from ..logging_setup import get_logger
from ..store import orders as repo
from ..store.db import Store
from ..store.orders import OpenClearance
from ..store.worker import StoreWorker
from ..util.timeutil import utc_now
from .account import AccountReader, CashRead, PositionsRead, QuoteRead
from .boundary import OrderAlert, OrderAlertHook, OrderAlertKind
from .exposure import SellRequest, UnreadableOrdersError, count_sells, store_position
from .instruments import InstrumentDirectory, prohibited_reasons
from .killswitch import CONVERSION_SEC_TYPE, KillSwitch
from .modes import Modes, write_refusal
from .quotes import QuotePolicy
from .state import BROKER_STATES

log = get_logger("execution.pretrade")

ZERO = Decimal(0)

#: The window the daily caps add up: the last 24 hours, not a calendar day.
CAP_WINDOW = timedelta(days=1)


class Reason(StrEnum):
    """Every way the gate refuses, one code each."""

    INTENT_UNKNOWN = "intent_unknown"
    INTENT_STATE = "intent_state"
    INTENT_EXPIRED = "intent_expired"
    TERMS_MISMATCH = "terms_mismatch"
    MODIFY_CHANGES_ORDER = "modify_changes_order"
    ORDER_NOT_INTENTS = "order_not_intents"
    CONVERSION_UNAVAILABLE = "conversion_unavailable"
    PAPER_GUARD = "paper_guard"
    MODE_UNREADABLE = "mode_unreadable"
    MODE_FORBIDS_WRITE = "mode_forbids_write"
    INTENT_MODE = "intent_mode"
    PROFILE_MISMATCH = "profile_mismatch"
    KILL_SWITCH_ON = "kill_switch_on"
    INSTRUMENT_UNKNOWN = "instrument_unknown"
    PROHIBITED_PRODUCT = "prohibited_product"
    INELIGIBLE = "ineligible"
    MANDATORY_CLOSE = "mandatory_close"
    APPROVAL_MISSING = "approval_missing"
    VENUE_NOT_TRADABLE = "venue_not_tradable"
    OPTION_EXPIRED = "option_expired"
    ACCOUNT_UNREADABLE = "account_unreadable"
    QUOTE_POLICY = "quote_policy"
    INSTRUMENT_HALTED = "instrument_halted"
    COLLAR = "collar"
    PROTECTIVE_SPEC = "protective_spec"
    HOLDING_UNMANAGED = "holding_unmanaged"
    HOLDING_UNASSIGNED = "holding_unassigned"
    HOLDING_OTHER_SLEEVE = "holding_other_sleeve"
    SHORT = "short"
    WRITTEN_OPTION = "written_option"
    FILLS_UNRECONCILED = "fills_unreconciled"
    CURRENCY_NOT_HELD = "currency_not_held"
    FX_RATE_UNAVAILABLE = "fx_rate_unavailable"
    WHATIF_REFUSED = "whatif_refused"
    MARGIN_USE = "margin_use"
    INSUFFICIENT_CASH = "insufficient_cash"
    CASH_UNACCOUNTED = "cash_unaccounted"
    NAV_UNAVAILABLE = "nav_unavailable"
    ORDER_NOTIONAL_CAP = "order_notional_cap"
    DAILY_NOTIONAL_CAP = "daily_notional_cap"
    DAILY_ORDER_CAP = "daily_order_cap"


@dataclass(frozen=True, slots=True)
class PretradeFailure:
    """One check that failed: its reason code and the words for the owner."""

    code: Reason
    reason: str


@dataclass(frozen=True, slots=True)
class PretradeRefusal:
    """Why an intent was not cleared: every failure found, the first one's code in ``code``."""

    intent_id: UUID
    failures: tuple[PretradeFailure, ...]

    @property
    def code(self) -> str:
        return self.failures[0].code.value

    @property
    def codes(self) -> tuple[str, ...]:
        return tuple(failure.code.value for failure in self.failures)

    @property
    def reason(self) -> str:
        return "; ".join(f"{f.code.value}: {f.reason}" for f in self.failures)


@dataclass(frozen=True, slots=True)
class _Request:
    """One evaluation: the stored intent, the write asked for, and what was found so far."""

    intent: OrderIntent
    coid: str
    action: ClearanceAction
    order_id: str | None
    terms: OrderTerms
    effect: RiskEffect
    assume: Mapping[str, Decimal]


@dataclass
class _Found:
    """What stage 1 and 2 learnt, for stage 3."""

    instrument: TradedInstrument | None = None
    underlying: TradedInstrument | None = None
    positions: PositionsRead | None = None
    live: LiveOrders | None = None
    trades: tuple[Fill, ...] = ()
    quote: QuoteRead | None = None
    underlying_quote: QuoteRead | None = None
    cash: CashRead | None = None
    whatif: WhatIf | None = None
    failures: list[PretradeFailure] = field(default_factory=list)

    def fail(self, code: Reason, reason: str) -> None:
        self.failures.append(PretradeFailure(code, reason))

    @property
    def traded(self) -> TradedInstrument:
        """The instrument, once stage 1 described it."""
        if self.instrument is None:  # pragma: no cover - stage 1 refuses an unknown instrument
            raise ObliskError("the instrument was not described")
        return self.instrument


def _notional(terms: OrderTerms, multiplier: Decimal) -> Decimal:
    """What the order commits at its limit, in its currency."""
    if terms.cash_quantity is not None:
        return terms.cash_quantity
    quantity = terms.quantity if terms.quantity is not None else ZERO
    return terms.price.limit * quantity * multiplier


class PretradeGate:
    """Clears an intent for one write, or refuses it with reason codes."""

    def __init__(
        self,
        settings: ObliskConfig,
        *,
        worker: StoreWorker,
        modes: Modes,
        kill_switch: KillSwitch,
        guard: AccountGuard,
        calendars: Calendars,
        directory: InstrumentDirectory,
        eligibility: EligibilityPolicy,
        reader: AccountReader,
        quotes: QuotePolicy | None = None,
        alert: OrderAlertHook | None = None,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._settings = settings
        self._worker = worker
        self._modes = modes
        self._kill = kill_switch
        self._guard = guard
        self._calendars = calendars
        self._directory = directory
        self._eligibility = eligibility
        self._reader = reader
        self._quotes = quotes if quotes is not None else QuotePolicy(settings)
        self._alert = alert
        self._now = now
        pretrade = settings.execution.pretrade
        caps = pretrade.caps
        self._order_fraction = caps.max_order_fraction_of_nav
        self._daily_fraction = caps.max_daily_fraction_of_nav
        self._max_orders = caps.max_orders_per_day
        self._floor = pretrade.cash_floor
        self._max_limit_offset = pretrade.protective.max_limit_offset_fraction
        self._held = frozenset(settings.execution.approval.sleeves)
        self._base = settings.account.base_currency
        self._close_sessions = settings.tenor.mandatory_close_trading_days
        # Every fill after a positions read in this window is in the trades IBKR serves.
        self._position_window = timedelta(days=settings.execution.orders.trades_lookup_days - 1)

    # ── the public surface ──

    async def clear(
        self,
        intent: OrderIntent,
        *,
        action: ClearanceAction = ClearanceAction.PLACE,
        order_id: str | None = None,
        terms: OrderTerms | None = None,
    ) -> PretradeClearance | PretradeRefusal:
        """A clearance for ``action`` on ``intent`` (``terms`` and ``order_id`` for a modify)."""
        answer = await self._evaluate(
            intent.intent_id, action=action, order_id=order_id, terms=terms, assume={}, issue=True
        )
        if answer is None:  # pragma: no cover - an issuing evaluation answers
            raise ObliskError("the gate neither cleared nor refused")
        return answer

    async def check(
        self,
        intent: OrderIntent,
        *,
        assume: Mapping[str, Decimal],
    ) -> PretradeRefusal | None:
        """Every check of a placement of ``intent``, issuing nothing; the broker orders in
        ``assume`` are counted at the size given (an exit beside a stop about to be cut)."""
        answer = await self._evaluate(
            intent.intent_id,
            action=ClearanceAction.PLACE,
            order_id=None,
            terms=None,
            assume=assume,
            issue=False,
        )
        return answer if isinstance(answer, PretradeRefusal) else None

    # ── the evaluation ──

    async def _evaluate(
        self,
        intent_id: UUID,
        *,
        action: ClearanceAction,
        order_id: str | None,
        terms: OrderTerms | None,
        assume: Mapping[str, Decimal],
        issue: bool,
    ) -> PretradeClearance | PretradeRefusal | None:
        stored = await self._worker.run(repo.get_intent, intent_id)
        if stored is None:
            return self._refuse(intent_id, Reason.INTENT_UNKNOWN, "no such intent in the store")
        intent = stored.intent
        asked = terms if terms is not None else intent.terms
        effect = (
            RiskEffect.REDUCES
            if asked.side is OrderSide.SELL and not asked.conversion
            else RiskEffect.ADDS
        )
        request = _Request(intent, stored.coid, action, order_id, asked, effect, assume)
        shape = await self._shape(request)
        if shape:
            return PretradeRefusal(intent_id, tuple(shape))
        found = await self._local(request)
        if found.failures:
            self._alert_option_close(request, found)
            return PretradeRefusal(intent_id, tuple(found.failures))
        await self._read(request, found)
        if found.failures:
            self._alert_option_close(request, found)
            return PretradeRefusal(intent_id, tuple(found.failures))
        outcome = await self._worker.run(self._commit, request, found, issue)
        if isinstance(outcome, list):
            return PretradeRefusal(intent_id, tuple(outcome))
        if outcome is not None:
            log.info(
                "cleared",
                extra={"intent": str(intent_id), "action": action.value, "effect": effect.value},
            )
        return outcome

    def _refuse(self, intent_id: UUID, code: Reason, reason: str) -> PretradeRefusal:
        return PretradeRefusal(intent_id, (PretradeFailure(code, reason),))

    async def _shape(self, request: _Request) -> list[PretradeFailure]:
        """The intent and the write asked for: refused at once, before any other check."""
        intent = request.intent
        failures: list[PretradeFailure] = []

        def fail(code: Reason, reason: str) -> None:
            failures.append(PretradeFailure(code, reason))

        if request.action is ClearanceAction.PLACE:
            if intent.state not in (IntentState.STAGED, IntentState.SUBMITTED):
                fail(Reason.INTENT_STATE, f"an intent in state {intent.state.value} is not placed")
            if request.order_id is not None or request.terms != intent.terms:
                fail(Reason.TERMS_MISMATCH, "a placement sends the intent's own terms")
        else:
            if intent.state not in BROKER_STATES:
                fail(
                    Reason.INTENT_STATE,
                    f"an intent in state {intent.state.value} has no order to modify",
                )
            asked, held = request.terms, intent.terms
            if (asked.conid, asked.side, asked.conversion, asked.quantity is None) != (
                held.conid,
                held.side,
                held.conversion,
                held.quantity is None,
            ):
                fail(
                    Reason.MODIFY_CHANGES_ORDER,
                    "a modify keeps the instrument, the side and the kind of sizing",
                )
            owner = (
                None
                if request.order_id is None
                else await self._worker.run(repo.intent_for_order, request.order_id)
            )
            if owner != intent.intent_id:
                fail(
                    Reason.ORDER_NOT_INTENTS,
                    f"order {request.order_id} was not placed for this intent",
                )
        if self._now() >= intent.expires_at:
            fail(Reason.INTENT_EXPIRED, "the intent has expired")
        if request.terms.conversion:
            fail(
                Reason.CONVERSION_UNAVAILABLE,
                "currency conversion is Session 5's: no conversion is cleared yet",
            )
        return failures

    async def _local(self, request: _Request) -> _Found:
        """Stage 1: everything decided without the broker."""
        intent, terms = request.intent, request.terms
        adds = request.effect is RiskEffect.ADDS
        instrument = self._directory.describe(terms.conid)
        found = _Found(instrument=instrument)
        try:
            self._guard.check_prefix()
        except AccountGuardError as exc:
            found.fail(Reason.PAPER_GUARD, str(exc))
        await self._mode(intent, found)
        if adds and await self._kill.is_on():
            found.fail(Reason.KILL_SWITCH_ON, "the kill switch blocks orders that add risk")
        if instrument is None:
            found.fail(Reason.INSTRUMENT_UNKNOWN, f"conid {terms.conid} is not described")
            return found
        if instrument.asset_class is AssetClass.OPTION and instrument.underlying_conid is not None:
            found.underlying = self._directory.describe(instrument.underlying_conid)
            if found.underlying is None:
                found.fail(
                    Reason.INSTRUMENT_UNKNOWN,
                    f"the underlying {instrument.underlying_conid} of {instrument.symbol} is not "
                    "described",
                )
                return found
        for reason in prohibited_reasons(instrument, found.underlying, self._settings):
            found.fail(Reason.PROHIBITED_PRODUCT, reason)
        mandatory_close = False
        if adds:
            verdict = self._eligibility.assess(instrument, underlying=found.underlying)
            if not verdict.eligible:
                found.fail(Reason.INELIGIBLE, verdict.reason)
            mandatory_close = verdict.mandatory_close
            await self._approval_check(intent, found)
        self._calendar_checks(instrument, found, mandatory_close=mandatory_close and adds)
        return found

    async def _mode(self, intent: OrderIntent, found: _Found) -> None:
        try:
            mode = await self._modes.current()
        except Exception as exc:  # any failure to read the mode refuses
            found.fail(Reason.MODE_UNREADABLE, f"the mode cannot be read ({type(exc).__name__})")
            return
        refusal = write_refusal(mode, self._modes.profile)
        if refusal is not None:
            found.fail(Reason.MODE_FORBIDS_WRITE, refusal)
        if mode is not intent.mode:
            found.fail(
                Reason.INTENT_MODE,
                f"the intent was made for {intent.mode.value}; the mode is {mode.value}",
            )
        if self._modes.profile != self._guard.profile:
            found.fail(
                Reason.PROFILE_MISMATCH,
                f"the modes run under profile {self._modes.profile}, the account guard under "
                f"{self._guard.profile}",
            )

    async def _approval_check(self, intent: OrderIntent, found: _Found) -> None:
        if not (intent.approval_required or intent.sleeve in self._held):
            return
        decision = await self._worker.run(repo.approval_for, intent.intent_id)
        if decision is None or not decision.approved:
            found.fail(
                Reason.APPROVAL_MISSING,
                f"the {intent.sleeve.value} intent waits for the owner's approval"
                if decision is None
                else f"the owner rejected the intent: {decision.reason}",
            )

    def _calendar_checks(
        self, instrument: TradedInstrument, found: _Found, *, mandatory_close: bool
    ) -> None:
        """The act-now view at this instant; an option date-critical inside its window."""
        calendars = self._calendars
        at = self._now()
        try:
            venue = calendars.venue_for_ibkr(instrument.listing_exchange)
            underlying = found.underlying
            if underlying is None or instrument.expiry is None:
                legs = [(venue, instrument.conid, False)]
            else:
                underlying_venue = calendars.venue_for_ibkr(underlying.listing_exchange)
                critical = self._option_dates(
                    instrument,
                    underlying,
                    instrument.expiry,
                    (venue, underlying_venue),
                    at,
                    found,
                    mandatory_close=mandatory_close,
                )
                if critical is None:
                    return
                legs = [
                    (venue, instrument.conid, critical),
                    (underlying_venue, underlying.conid, critical),
                ]
            blocked = False
            for mic, conid, critical in legs:
                status = calendars.tradability(mic, at, conid=conid, date_critical=critical)
                if status.reason is not None:
                    blocked = True
                    found.fail(
                        Reason.VENUE_NOT_TRADABLE,
                        f"{mic} at {at.isoformat()}: {status.reason.value}"
                        + (f" ({status.detail})" if status.detail else ""),
                    )
            if len(legs) > 1 and not blocked:
                day = calendars.session_day(venue, at)
                windows = calendars.common_windows(day, *((m, c) for m, c, _ in legs))
                if not any(window.contains(at) for window in windows):
                    found.fail(
                        Reason.VENUE_NOT_TRADABLE,
                        f"{at.isoformat()} is outside the windows {venue} and {legs[1][0]} share",
                    )
        except CalendarError as exc:
            found.fail(Reason.VENUE_NOT_TRADABLE, f"the calendars cannot judge it: {exc}")

    def _option_dates(
        self,
        option: TradedInstrument,
        underlying: TradedInstrument,
        expiry: date,
        venues: tuple[str, str],
        at: datetime,
        found: _Found,
        *,
        mandatory_close: bool,
    ) -> bool | None:
        """Whether an option order at ``at`` is date-critical; ``None`` when it is refused."""
        calendars = self._calendars
        venue, underlying_venue = venues
        day = calendars.session_day(venue, at)
        last = calendars.last_tradable_session(
            venue,
            underlying_venue,
            expiry,
            instrument_conid=option.conid,
            underlying_conid=underlying.conid,
        ).session
        if day > last:
            found.fail(
                Reason.OPTION_EXPIRED,
                f"{option.symbol}'s last tradable session was {last.isoformat()}",
            )
            return None
        close_by = calendars.session_before(
            venue,
            underlying_venue,
            last,
            self._close_sessions,
            instrument_conid=option.conid,
            underlying_conid=underlying.conid,
        )
        if mandatory_close and day >= close_by:
            found.fail(
                Reason.MANDATORY_CLOSE,
                f"{option.symbol} must be closed by {close_by.isoformat()}: an option on an "
                "ineligible ETP is not opened inside its mandatory-close window",
            )
        return day >= close_by

    async def _read(self, request: _Request, found: _Found) -> None:
        """Stage 2: the broker reads, the quote policy and the collar."""
        reader = self._reader
        adds = request.effect is RiskEffect.ADDS
        try:
            found.positions = await reader.positions()
            found.live = await reader.live_orders()
            found.trades = await reader.trades()
            if adds:
                found.cash = await reader.cash()
        except (IbkrError, ObliskError) as exc:
            found.fail(Reason.ACCOUNT_UNREADABLE, f"the account cannot be read: {exc}")
            return
        if not found.live.complete:
            found.fail(Reason.ACCOUNT_UNREADABLE, "the live orders are not a complete snapshot")
        await self._quote_checks(request, found)
        if adds and not found.failures:
            try:
                found.whatif = await reader.whatif(request.terms)
            except (IbkrError, ObliskError) as exc:
                found.fail(Reason.WHATIF_REFUSED, f"the whatif failed: {exc}")

    async def _quote_checks(self, request: _Request, found: _Found) -> None:
        instrument = found.traded
        option = instrument.asset_class is AssetClass.OPTION
        try:
            found.quote = await self._reader.quote(instrument.conid, option=option)
            if found.underlying is not None:
                found.underlying_quote = await self._reader.quote(
                    found.underlying.conid, option=False
                )
        except (IbkrError, ObliskError) as exc:
            found.fail(Reason.QUOTE_POLICY, f"the quote cannot be read: {exc}")
            return
        quote = found.quote
        if quote is not None and quote.halted:
            found.fail(Reason.INSTRUMENT_HALTED, f"IBKR reports {instrument.symbol} halted")
        evidence = None if quote is None else quote.evidence
        underlying = None if found.underlying_quote is None else found.underlying_quote.evidence
        check = self._quotes.check(
            instrument.asset_class, evidence, now=self._now(), underlying=underlying
        )
        if not check.ok or evidence is None:
            found.fail(Reason.QUOTE_POLICY, check.reason)
            return
        sigma = None if quote is None else quote.sigma
        terms = request.terms
        protective = request.effect is RiskEffect.REDUCES and terms.price.kind == "stop_limit"
        collar = self._quotes.within_collar(terms, evidence, sigma=sigma)
        if protective:
            self._protective_spec(terms, collar.ok, collar.reason, found)
        elif not collar.ok:
            found.fail(Reason.COLLAR, collar.reason)

    def _protective_spec(self, terms: OrderTerms, rests: bool, why: str, found: _Found) -> None:
        price = terms.price
        if price.kind != "stop_limit":  # pragma: no cover - the caller checks
            return
        if not rests:
            found.fail(Reason.PROTECTIVE_SPEC, why)
        if price.limit > price.stop:
            found.fail(
                Reason.PROTECTIVE_SPEC,
                f"the limit {price.limit} sits above the stop {price.stop}: a sell stop-limit's "
                "limit is at or below its stop",
            )
        floor = price.stop * (1 - self._max_limit_offset)
        if price.limit < floor:
            found.fail(
                Reason.PROTECTIVE_SPEC,
                f"the limit {price.limit} is more than {self._max_limit_offset} below the stop "
                f"{price.stop} (at least {floor})",
            )

    def _alert_option_close(self, request: _Request, found: _Found) -> None:
        """An option close refused for want of a real-time quote reaches the owner."""
        instrument = found.instrument
        if (
            instrument is None
            or request.effect is not RiskEffect.REDUCES
            or instrument.asset_class is not AssetClass.OPTION
            or not any(f.code is Reason.QUOTE_POLICY for f in found.failures)
        ):
            return
        detail = (
            f"a close of option {instrument.symbol} (conid {instrument.conid}) was "
            "refused for want of a real-time quote; an in-the-money option left to expiry may "
            "be exercised"
        )
        alert = OrderAlert(
            kind=OrderAlertKind.OPTION_CLOSE_BLOCKED,
            intent_id=request.intent.intent_id,
            detail=detail,
            at=self._now(),
        )
        log.warning("order alert", extra={"kind": alert.kind.value, "intent": str(alert.intent_id)})
        if self._alert is None:
            return
        try:
            self._alert(alert)
        except Exception:
            log.exception("the order alert hook failed")

    # ── stage 3: the store, in one transaction ──

    def _commit(
        self, store: Store, request: _Request, found: _Found, issue: bool
    ) -> PretradeClearance | list[PretradeFailure] | None:
        positions, live = found.positions, found.live
        if positions is None or live is None:  # pragma: no cover - stage 2 refused
            raise ObliskError("the broker reads are missing")
        with store.transaction():
            repo.upsert_fills(store, found.trades)
            repo.record_positions(store, positions.positions, observed_at=positions.observed_at)
            now = self._now()
            failures = self._holdings(store, request, positions)
            clearances = repo.open_clearances(store)
            if request.effect is RiskEffect.REDUCES:
                failures += self._no_short(store, request, found, clearances, now)
            notional = _notional(request.terms, found.traded.multiplier)
            notional_base: Decimal | None = None
            if request.effect is RiskEffect.ADDS:
                cash_failures, notional_base = self._cash_and_caps(
                    store, request, found, clearances, notional, now
                )
                failures += cash_failures
            if failures:
                return failures
            if not issue:
                return None
            return self._issue(store, request, found, notional, notional_base, now)

    def _holdings(
        self, store: Store, request: _Request, positions: PositionsRead
    ) -> list[PretradeFailure]:
        conid = request.terms.conid
        assignment = repo.holding_assignment(store, conid)
        held = positions.quantity(conid)
        if assignment is None:
            if held != 0:
                return [
                    PretradeFailure(
                        Reason.HOLDING_UNASSIGNED,
                        f"conid {conid} is held ({held}) but assigned to no sleeve: it is "
                        "unmanaged until the owner assigns it",
                    )
                ]
            return []
        if assignment.owner == UNMANAGED:
            return [
                PretradeFailure(
                    Reason.HOLDING_UNMANAGED,
                    f"conid {conid} is unmanaged ({assignment.reason}): never traded until the "
                    "owner assigns it",
                )
            ]
        if assignment.owner != request.intent.sleeve:
            return [
                PretradeFailure(
                    Reason.HOLDING_OTHER_SLEEVE,
                    f"conid {conid} belongs to the {assignment.owner} sleeve, not "
                    f"{request.intent.sleeve.value}",
                )
            ]
        return []

    def _no_short(
        self,
        store: Store,
        request: _Request,
        found: _Found,
        clearances: Sequence[OpenClearance],
        now: datetime,
    ) -> list[PretradeFailure]:
        """I2: every sell that could still reach the broker within the long position."""
        positions, live = found.positions, found.live
        if positions is None or live is None:  # pragma: no cover - stage 2 refused
            return []
        terms = request.terms
        conid = terms.conid
        option = found.traded.asset_class is AssetClass.OPTION
        code = Reason.WRITTEN_OPTION if option else Reason.SHORT
        since = now - self._position_window
        reads = repo.position_reads(store, since=since, as_of=now)
        fills = repo.fills_for_conid(store, conid, since=since, as_of=now)
        from_store = store_position(reads, fills, conid)
        broker = positions.quantity(conid)
        position = broker if from_store is None else min(broker, from_store)
        coids = {entry.coid for entry in clearances} | {
            order.coid for order in live.orders if order.coid is not None
        }
        recorded = repo.fills_by_coid(store, coids)
        filled = {
            coid: sum((fill.quantity for fill in rows), ZERO) for coid, rows in recorded.items()
        }
        failures: list[PretradeFailure] = []
        for order in live.orders:
            if (
                order.conid == conid
                and order.side is OrderSide.SELL
                and order.coid is not None
                and (order.filled_quantity or ZERO) > filled.get(order.coid, ZERO)
            ):
                failures.append(
                    PretradeFailure(
                        Reason.FILLS_UNRECONCILED,
                        f"order {order.order_id} reports {order.filled_quantity} filled, the "
                        f"store {filled.get(order.coid, ZERO)}: the position is not accounted",
                    )
                )
        live_orders = [
            order.model_copy(update={"remaining_quantity": request.assume[order.order_id]})
            if order.order_id in request.assume
            else order
            for order in live.orders
        ]
        assumed = [
            entry
            for entry in clearances
            if not (
                entry.clearance.action is ClearanceAction.MODIFY
                and entry.clearance.order_id in request.assume
            )
        ]
        quantity = terms.quantity if terms.quantity is not None else ZERO
        sell = SellRequest(
            intent_id=request.intent.intent_id,
            coid=request.coid,
            conid=conid,
            quantity=quantity,
            order_id=request.order_id,
        )
        try:
            count = count_sells(
                sell, live=live_orders, clearances=assumed, filled_by_coid=filled, now=now
            )
        except UnreadableOrdersError as exc:
            failures.append(PretradeFailure(Reason.ACCOUNT_UNREADABLE, str(exc)))
            return failures
        if position <= 0 or count.total > position:
            what = (
                "an option sold beyond the long position is a written option"
                if option
                else ("a sell beyond the long position is a short")
            )
            failures.append(
                PretradeFailure(
                    code,
                    f"{what}: sells {count.total} against a position of {position} "
                    f"(broker {broker}, store {from_store}; {', '.join(count.parts)})",
                )
            )
        return failures

    def _cash_and_caps(
        self,
        store: Store,
        request: _Request,
        found: _Found,
        clearances: Sequence[OpenClearance],
        notional: Decimal,
        now: datetime,
    ) -> tuple[list[PretradeFailure], Decimal | None]:
        """No leverage, the cash in the order's currency, and the caps."""
        failures: list[PretradeFailure] = []

        def fail(code: Reason, reason: str) -> None:
            failures.append(PretradeFailure(code, reason))

        cash, whatif, live = found.cash, found.whatif, found.live
        if cash is None or whatif is None or live is None:  # pragma: no cover - stage 2 refused
            raise ObliskError("the ledger or the whatif was not read")
        currency = found.traded.currency
        line = cash.line(currency)
        rate = None if line is None else line.rate_to_base
        if line is None:
            fail(
                Reason.CURRENCY_NOT_HELD,
                f"the account holds no {currency}; funding by conversion is Session 5's",
            )
        elif rate is None:
            fail(Reason.FX_RATE_UNAVAILABLE, f"the ledger gives no rate from {currency} to base")
        cost = whatif.total if whatif.total is not None else whatif.amount
        if whatif.error is not None:
            fail(Reason.WHATIF_REFUSED, f"the whatif refused the order: {whatif.error}")
        elif cost is None or cost <= 0:
            fail(Reason.WHATIF_REFUSED, "the whatif reports no readable cost")
        elif whatif.currency is not None and whatif.currency != currency:
            fail(
                Reason.WHATIF_REFUSED,
                f"the whatif's cost is in {whatif.currency}, the order's currency is {currency}",
            )
        notional_base = None if rate is None else notional * rate
        if line is None or rate is None or failures or cost is None:
            return failures, notional_base
        foreign = self._foreign_buys(store, live)
        if foreign:
            fail(
                Reason.CASH_UNACCOUNTED,
                f"working buys the bot did not place commit unknown cash: {', '.join(foreign)}",
            )
        committed = self._committed(store, request, clearances, currency, now)
        floor = self._floor / rate
        if line.cash - cost < 0:
            fail(
                Reason.MARGIN_USE,
                f"the whatif's cost {cost} {currency} exceeds the {line.cash} {currency} held: "
                "the order would borrow",
            )
        free = line.cash - committed - cost - floor
        if free < 0:
            fail(
                Reason.INSUFFICIENT_CASH,
                f"{line.cash} {currency} held, {committed:.2f} committed to open buys, the order "
                f"costs {cost} and {floor:.2f} must remain: short by {-free:.2f}",
            )
        failures += self._caps_check(store, request, cash, notional_base, now)
        return failures, notional_base

    def _foreign_buys(self, store: Store, live: LiveOrders) -> list[str]:
        foreign = []
        for order in live.orders:
            buys = order.side is not OrderSide.SELL or (
                (order.sec_type or "").upper() == CONVERSION_SEC_TYPE
            )
            if not is_live(order) or not buys:
                continue
            if order.coid is None or repo.intent_by_coid(store, order.coid) is None:
                foreign.append(order.order_id)
        return foreign

    def _committed(
        self,
        store: Store,
        request: _Request,
        clearances: Sequence[OpenClearance],
        currency: str,
        now: datetime,
    ) -> Decimal:
        """Cash the other open buys in ``currency`` may still take: one figure per open intent,
        the largest any of its clearances that may still be or have been sent commits, less what
        it filled."""
        per_intent: dict[UUID, tuple[str, Decimal, OrderTerms]] = {}
        for entry in clearances:
            clearance = entry.clearance
            if (
                clearance.intent_id == request.intent.intent_id
                or clearance.effect is not RiskEffect.ADDS
                or clearance.terms.side is not OrderSide.BUY
                # A clearance that names no currency is counted against every one.
                or clearance.currency not in (None, currency)
            ):
                continue
            if entry.used_at is None and clearance.expires_at <= now:
                continue
            amount = clearance.notional
            if amount is None:
                amount = _notional(clearance.terms, Decimal(1))
            held = per_intent.get(clearance.intent_id)
            if held is None or amount > held[1]:
                per_intent[clearance.intent_id] = (entry.coid, amount, clearance.terms)
        fills = repo.fills_by_coid(store, [coid for coid, _, _ in per_intent.values()])
        total = ZERO
        for coid, amount, terms in per_intent.values():
            done = fills.get(coid, [])
            if terms.quantity is not None:
                filled = sum((fill.quantity for fill in done), ZERO)
                total += amount * max(ZERO, 1 - filled / terms.quantity)
            else:
                spent = sum((fill.quantity * fill.price for fill in done), ZERO)
                total += max(ZERO, amount - spent)
        return total

    def _caps_check(
        self,
        store: Store,
        request: _Request,
        cash: CashRead,
        notional_base: Decimal | None,
        now: datetime,
    ) -> list[PretradeFailure]:
        if cash.nav is None or cash.nav <= 0:
            return [
                PretradeFailure(
                    Reason.NAV_UNAVAILABLE,
                    f"the NAV is not readable in the base currency ({cash.nav_refusal})",
                )
            ]
        if notional_base is None:  # pragma: no cover - the rate was checked
            return []
        failures: list[PretradeFailure] = []
        order_cap = cash.nav * self._order_fraction
        if notional_base > order_cap:
            failures.append(
                PretradeFailure(
                    Reason.ORDER_NOTIONAL_CAP,
                    f"the order commits {notional_base:.2f} {self._base}, above the per-order "
                    f"cap of {order_cap:.2f}",
                )
            )
        own = request.intent.intent_id
        window = repo.clearances_issued(store, since=now - CAP_WINDOW, as_of=now)
        per_intent: dict[UUID, Decimal] = {}
        placed: set[UUID] = set()
        for entry in window:
            clearance = entry.clearance
            if clearance.effect is not RiskEffect.ADDS:
                continue
            if entry.used_at is None and clearance.expires_at <= now:
                continue
            amount = clearance.notional_base
            if amount is None:
                failures.append(
                    PretradeFailure(
                        Reason.DAILY_NOTIONAL_CAP,
                        f"clearance {clearance.clearance_id} carries no notional: the day's "
                        "total cannot be known",
                    )
                )
                continue
            per_intent[clearance.intent_id] = max(per_intent.get(clearance.intent_id, ZERO), amount)
            if clearance.action is ClearanceAction.PLACE:
                placed.add(clearance.intent_id)
        per_intent[own] = max(per_intent.get(own, ZERO), notional_base)
        if request.action is ClearanceAction.PLACE:
            placed.add(own)
        day_total = sum(per_intent.values(), ZERO)
        day_cap = cash.nav * self._daily_fraction
        if day_total > day_cap:
            failures.append(
                PretradeFailure(
                    Reason.DAILY_NOTIONAL_CAP,
                    f"{day_total:.2f} {self._base} cleared in 24 hours with this order, above "
                    f"the daily cap of {day_cap:.2f}",
                )
            )
        if len(placed) > self._max_orders:
            failures.append(
                PretradeFailure(
                    Reason.DAILY_ORDER_CAP,
                    f"{len(placed)} orders adding risk in 24 hours with this one, above "
                    f"{self._max_orders}",
                )
            )
        return failures

    def _issue(
        self,
        store: Store,
        request: _Request,
        found: _Found,
        notional: Decimal,
        notional_base: Decimal | None,
        now: datetime,
    ) -> PretradeClearance | list[PretradeFailure]:
        quote = found.quote
        if quote is None:  # pragma: no cover - the quote policy refused
            return [PretradeFailure(Reason.QUOTE_POLICY, "no quote")]
        evidence: list[QuoteEvidence] = [quote.evidence]
        if found.underlying_quote is not None:
            evidence.append(found.underlying_quote.evidence)
        instrument = found.traded
        rule = self._quotes.rule_for(instrument.asset_class)
        if rule is None or isinstance(rule, NavPriced):  # pragma: no cover - refused in stage 2
            return [PretradeFailure(Reason.QUOTE_POLICY, "no quote policy")]
        age = timedelta(seconds=rule.max_quote_age_seconds)
        expires = min([request.intent.expires_at, *(e.observed_at + age for e in evidence)])
        if expires <= now:
            return [PretradeFailure(Reason.QUOTE_POLICY, "the quote went stale before clearing")]
        clearance = PretradeClearance(
            clearance_id=uuid.uuid4(),
            intent_id=request.intent.intent_id,
            action=request.action,
            order_id=request.order_id,
            terms=request.terms,
            effect=request.effect,
            asset_class=instrument.asset_class,
            quote=quote.evidence,
            underlying_quote=None
            if found.underlying_quote is None
            else found.underlying_quote.evidence,
            sigma=quote.sigma,
            issued_at=now,
            expires_at=expires,
            currency=instrument.currency,
            notional=notional,
            notional_base=notional_base,
        )
        repo.record_clearance(store, clearance)
        return clearance
