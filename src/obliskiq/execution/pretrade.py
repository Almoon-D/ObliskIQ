"""The pre-trade gate: the only issuer of a :class:`~obliskiq.domain.orders.PretradeClearance`.

No placement or modify reaches the broker without a clearance (I1, SESSION_PROMPTS Session 4
item 4). The gate clears an intent for one write only when every check passes, and otherwise
refuses it with a reason code per failure (:class:`Reason`). It works from the stored intent,
never from the object a caller holds.

**What it checks.** Three stages, each reporting every failure it finds:

1. *Local*, before the broker is touched: the intent (stored, in a state that may be written,
   unexpired, the action's terms); the paper guard (profile paper ⇒ a paper-prefix account); the
   mode (the intent's, permitting writes under this profile); the kill switch and, for an order
   that adds risk, a reconciliation that is not green (it freezes new risk); the instrument
   described; prohibited products; buy-side eligibility through the jurisdiction's
   :class:`EligibilityPolicy` (the mandatory close for an option on an ineligible ETP); approval;
   the calendars' act-now view at this instant (``Calendars.tradability``; an option also inside
   ``common_windows`` with its underlying's venue, and date-critical inside its mandatory-close
   window).
2. *Broker reads*: positions, live orders (a complete snapshot), trades, the quote policy and the
   collar (a protective stop: its intent's protective spec instead), and, for an order that adds
   risk, the ledger and a whatif.
3. *Store*, in one transaction that also records the clearance, so two processes see each
   other: the holdings registry (``unmanaged`` never traded, a held conid without a sleeve
   refused), no short (I2, ``execution/exposure.py``), and, for an order that adds risk, the
   cash in its currency against every open commitment (working conversions out of it included)
   and the floor, no borrowing by the whatif's own cost, and the caps per order, per 24 hours and
   in count.

**A currency conversion** (``execution/fx.py``) is checked on its own path: a configured pair, the
paper guard, the mode, the kill switch and the freeze; the reads; the pair's quote under the
quote policy and the collar; then the cash it converts from against its open commitments and the
floor, and the single-foreign-currency cap (lite's ``positions.max_single_foreign_currency_pct``)
on the currency it buys: that currency's cash, the conversion, the conversions still working into
it and the positions quoted in it, against the NAV. It funds a trade that passes its own checks,
so it carries no eligibility, approval, calendar or notional cap of its own (DECISIONS D98).

**Class by effect (I3).** A sell of a managed long position, within it, reduces risk; every buy
and every conversion adds risk. For an order that reduces risk the gate waives exactly the kill
switch, the reconciliation freeze, the notional caps, the order count, buy-side eligibility, the
cash and no-margin check, and approval; every other check applies, and a sell stop-limit is
checked against its intent's protective spec instead of the collar: its stop rests below the
collar's band and its limit within the spec's offset below the stop. A modify of a stop the market
has reached (the reference at or below its stop) reprices a working limit: its limit is held to
the sell collar, or, when the owner enabled the emergency limit, to that fraction below the bid
(DECISIONS D67). A refused option close for want of a real-time quote alerts the owner: an
option left to expiry may be exercised.

**The clearance** carries the effect, the quotes it was checked against and the collar's sigma
(the reply policy decides IBKR's no-market-data warning from them), the order's notional, and
expires with the earliest quote it rests on (and never after the intent).

:meth:`PretradeGate.check` runs the same checks without issuing anything, counting named orders
at an assumed size: an exit that needs its protective stop resized passes its other checks
before the stop is touched (I5, ``execution/exits.py``). In shadow mode the executor asks it
with ``simulated``: every check but the mode's refusal to write, for a fill it only simulates.

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
    ExecutionMode,
    Fill,
    IntentState,
    LimitPrice,
    OrderIntent,
    OrderSide,
    OrderTerms,
    PretradeClearance,
    QuoteEvidence,
    ReconcileStatus,
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
from .boundary import OrderAlert, OrderAlertHook, OrderAlertKind, raise_alert
from .exposure import SellRequest, UnreadableOrdersError, count_sells, store_position
from .fx import ConversionLeg, conversion_leg
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
    CONVERSION_PAIR = "conversion_pair"
    PAPER_GUARD = "paper_guard"
    MODE_UNREADABLE = "mode_unreadable"
    MODE_FORBIDS_WRITE = "mode_forbids_write"
    INTENT_MODE = "intent_mode"
    PROFILE_MISMATCH = "profile_mismatch"
    KILL_SWITCH_ON = "kill_switch_on"
    RECONCILIATION_FROZEN = "reconciliation_frozen"
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
    CURRENCY_CAP = "currency_cap"
    EXPOSURE_UNREADABLE = "exposure_unreadable"
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
    simulated: bool = False


@dataclass
class _Found:
    """What stage 1 and 2 learnt, for stage 3."""

    instrument: TradedInstrument | None = None
    leg: ConversionLeg | None = None
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
        protective = settings.execution.protective
        self._max_spec_offset = protective.limit_offset.max_fraction
        self._emergency_on = protective.gap.emergency.enabled
        self._emergency_fraction = protective.gap.emergency.fraction_below_bid
        self._pairs = tuple(settings.execution.currency.pairs)
        self._max_currency = settings.positions.max_single_foreign_currency_pct
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
        simulated: bool = False,
    ) -> PretradeRefusal | None:
        """Every check of a placement of ``intent``, issuing nothing; the broker orders in
        ``assume`` are counted at the size given (an exit beside a stop about to be cut).
        ``simulated``: a shadow intent in shadow mode, whose fill is only simulated, passes the
        mode's refusal to write and nothing else."""
        answer = await self._evaluate(
            intent.intent_id,
            action=ClearanceAction.PLACE,
            order_id=None,
            terms=None,
            assume=assume,
            issue=False,
            simulated=simulated,
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
        simulated: bool = False,
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
        request = _Request(
            intent, stored.coid, action, order_id, asked, effect, assume, simulated and not issue
        )
        shape = await self._shape(request)
        if shape:
            return PretradeRefusal(intent_id, tuple(shape))
        if asked.conversion:
            found = await self._local_conversion(request)
        else:
            found = await self._local(request)
        if found.failures:
            self._alert_option_close(request, found)
            return PretradeRefusal(intent_id, tuple(found.failures))
        if asked.conversion:
            await self._read_conversion(request, found)
        else:
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
        return failures

    async def _local(self, request: _Request) -> _Found:
        """Stage 1: everything decided without the broker."""
        intent, terms = request.intent, request.terms
        adds = request.effect is RiskEffect.ADDS
        instrument = self._directory.describe(terms.conid)
        found = _Found(instrument=instrument)
        await self._guards(request, found)
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

    async def _guards(self, request: _Request, found: _Found) -> None:
        """The paper guard, the mode and, for an order that adds risk, the kill switch and the
        reconciliation freeze."""
        try:
            self._guard.check_prefix()
        except AccountGuardError as exc:
            found.fail(Reason.PAPER_GUARD, str(exc))
        await self._mode(request, found)
        if request.effect is not RiskEffect.ADDS:
            return
        if await self._kill.is_on():
            found.fail(Reason.KILL_SWITCH_ON, "the kill switch blocks orders that add risk")
        latest = await self._worker.run(repo.latest_reconciliation)
        if latest is not None and latest.status is not ReconcileStatus.GREEN:
            found.fail(
                Reason.RECONCILIATION_FROZEN,
                f"the latest reconciliation ({latest.at.isoformat()}) is {latest.status.value}: "
                "new risk waits for a green one",
            )

    async def _mode(self, request: _Request, found: _Found) -> None:
        intent = request.intent
        try:
            mode = await self._modes.current()
        except Exception as exc:  # any failure to read the mode refuses
            found.fail(Reason.MODE_UNREADABLE, f"the mode cannot be read ({type(exc).__name__})")
            return
        refusal = write_refusal(mode, self._modes.profile)
        simulated = request.simulated and mode is ExecutionMode.SHADOW
        if refusal is not None and not simulated:
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

    async def _local_conversion(self, request: _Request) -> _Found:
        """Stage 1 of a conversion: a configured pair, the paper guard, the mode, the kill switch
        and the freeze."""
        found = _Found(leg=conversion_leg(self._pairs, request.terms))
        await self._guards(request, found)
        if found.leg is None:
            found.fail(
                Reason.CONVERSION_PAIR,
                f"conid {request.terms.conid} is not a currency pair in execution.currency.pairs",
            )
        return found

    async def _read_conversion(self, request: _Request, found: _Found) -> None:
        """Stage 2 of a conversion: the account, the pair's quote under the quote policy and the
        collar, and a whatif."""
        reader = self._reader
        try:
            found.positions = await reader.positions()
            found.live = await reader.live_orders()
            found.trades = await reader.trades()
            found.cash = await reader.cash()
            found.quote = await reader.quote(request.terms.conid, option=False)
        except (IbkrError, ObliskError) as exc:
            found.fail(Reason.ACCOUNT_UNREADABLE, f"the account or the pair cannot be read: {exc}")
            return
        if not found.live.complete:
            found.fail(Reason.ACCOUNT_UNREADABLE, "the live orders are not a complete snapshot")
        evidence = None if found.quote is None else found.quote.evidence
        check = self._quotes.check(AssetClass.FX, evidence, now=self._now())
        if not check.ok or found.quote is None or evidence is None:
            found.fail(Reason.QUOTE_POLICY, check.reason)
            return
        collar = self._quotes.within_collar(request.terms, evidence, sigma=found.quote.sigma)
        if not collar.ok:
            found.fail(Reason.COLLAR, collar.reason)
        if not found.failures:
            try:
                found.whatif = await reader.whatif(request.terms)
            except (IbkrError, ObliskError) as exc:
                found.fail(Reason.WHATIF_REFUSED, f"the whatif failed: {exc}")

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
        if request.effect is RiskEffect.REDUCES and terms.price.kind == "stop_limit":
            self._protective_spec(request, evidence, sigma, found)
            return
        collar = self._quotes.within_collar(terms, evidence, sigma=sigma)
        if not collar.ok:
            found.fail(Reason.COLLAR, collar.reason)

    def _protective_spec(
        self, request: _Request, quote: QuoteEvidence, sigma: float | None, found: _Found
    ) -> None:
        """A sell stop-limit that reduces risk, against its intent's protective spec."""
        terms = request.terms
        price = terms.price
        if price.kind != "stop_limit":  # pragma: no cover - the caller checks
            return
        spec = request.intent.protective
        if spec is None:
            found.fail(
                Reason.PROTECTIVE_SPEC,
                "a sell stop-limit that reduces risk rests by its intent's protective spec, and "
                "this intent carries none",
            )
            return
        if spec.limit_offset_fraction > self._max_spec_offset:
            found.fail(
                Reason.PROTECTIVE_SPEC,
                f"the spec's limit offset {spec.limit_offset_fraction} is beyond "
                f"{self._max_spec_offset}: a limit that deep acts as a market order",
            )
        if price.limit > price.stop:
            found.fail(
                Reason.PROTECTIVE_SPEC,
                f"the limit {price.limit} sits above the stop {price.stop}: a sell stop-limit's "
                "limit is at or below its stop",
            )
        reference = quote.reference()
        if (
            request.action is ClearanceAction.MODIFY
            and reference is not None
            and reference <= price.stop
        ):
            self._triggered(terms, price.limit, quote, sigma, found)
            return
        collar = self._quotes.within_collar(terms, quote, sigma=sigma)
        if not collar.ok:
            found.fail(Reason.PROTECTIVE_SPEC, collar.reason)
        floor = spec.lowest_limit(price.stop)
        if price.limit < floor:
            found.fail(
                Reason.PROTECTIVE_SPEC,
                f"the limit {price.limit} is more than {spec.limit_offset_fraction} below the "
                f"stop {price.stop} (at least {floor})",
            )

    def _triggered(
        self,
        terms: OrderTerms,
        limit: Decimal,
        quote: QuoteEvidence,
        sigma: float | None,
        found: _Found,
    ) -> None:
        """A modify of a stop the market has reached reprices a working sell limit: inside the
        collar, or within the emergency limit the owner enabled, never lower."""
        as_limit = terms.model_copy(update={"price": LimitPrice(limit=limit)})
        collar = self._quotes.within_collar(as_limit, quote, sigma=sigma)
        if collar.ok:
            return
        if self._emergency_on:
            if quote.bid is None:
                found.fail(Reason.PROTECTIVE_SPEC, "no bid to set the emergency limit from")
                return
            deepest = quote.bid * (1 - self._emergency_fraction)
            if limit < deepest:
                found.fail(
                    Reason.PROTECTIVE_SPEC,
                    f"the triggered stop's limit {limit} is below the emergency limit {deepest}",
                )
            return
        found.fail(
            Reason.PROTECTIVE_SPEC,
            f"a triggered stop is repriced inside the collar ({collar.reason}); a limit beyond "
            "it is the emergency limit, which is off",
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
        raise_alert(self._alert, alert)

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
            clearances = repo.open_clearances(store)
            if found.leg is not None:
                failures, amount, amount_base = self._conversion_cash(
                    store, request, found, clearances, now
                )
                if failures:
                    return failures
                if not issue:
                    return None
                return self._issue(store, request, found, amount, amount_base, now)
            failures = self._holdings(store, request, positions)
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

    def _conversion_cash(
        self,
        store: Store,
        request: _Request,
        found: _Found,
        clearances: Sequence[OpenClearance],
        now: datetime,
    ) -> tuple[list[PretradeFailure], Decimal, Decimal | None]:
        """A conversion: the cash it converts from, against every open commitment in that
        currency and the floor, and the single-foreign-currency cap on what it buys."""
        failures: list[PretradeFailure] = []

        def fail(code: Reason, reason: str) -> None:
            failures.append(PretradeFailure(code, reason))

        leg, cash, whatif, live = found.leg, found.cash, found.whatif, found.live
        amount = request.terms.cash_quantity
        if leg is None or cash is None or whatif is None or live is None or amount is None:
            raise ObliskError("the conversion was not read")  # pragma: no cover - stage 2 refused
        source = cash.line(leg.source)
        rate = None if source is None else source.rate_to_base
        if source is None:
            fail(Reason.CURRENCY_NOT_HELD, f"the account holds no {leg.source} to convert from")
        elif rate is None:
            fail(Reason.FX_RATE_UNAVAILABLE, f"the ledger gives no rate from {leg.source} to base")
        if whatif.error is not None:
            fail(Reason.WHATIF_REFUSED, f"the whatif refused the conversion: {whatif.error}")
        amount_base = None if rate is None else amount * rate
        if source is None or rate is None or failures:
            return failures, amount, amount_base
        foreign = self._foreign_buys(store, live)
        if foreign:
            fail(
                Reason.CASH_UNACCOUNTED,
                f"working buys the bot did not place commit unknown cash: {', '.join(foreign)}",
            )
        if source.cash - amount < 0:
            fail(
                Reason.MARGIN_USE,
                f"converting {amount} {leg.source} out of the {source.cash} held would borrow",
            )
        committed = self._committed(store, request, clearances, leg.source, now)
        floor = self._floor / rate
        free = source.cash - committed - amount - floor
        if free < 0:
            fail(
                Reason.INSUFFICIENT_CASH,
                f"{source.cash} {leg.source} held, {committed:.2f} committed to open orders, the "
                f"conversion takes {amount} and {floor:.2f} must remain: short by {-free:.2f}",
            )
        incoming = self._incoming(store, request, clearances, leg.target, now)
        failures += self._currency_cap(leg, amount, incoming, found)
        return failures, amount, amount_base

    def _incoming(
        self,
        store: Store,
        request: _Request,
        clearances: Sequence[OpenClearance],
        currency: str,
        now: datetime,
    ) -> Decimal:
        """What the other conversions into ``currency`` that may still be or have been sent will
        bring in at their limits, less what they filled."""
        per_intent: dict[UUID, tuple[str, ConversionLeg, OrderTerms]] = {}
        for entry in clearances:
            terms = entry.clearance.terms
            leg = conversion_leg(self._pairs, terms) if terms.conversion else None
            if (
                entry.clearance.intent_id == request.intent.intent_id
                or leg is None
                or leg.target != currency
                or (entry.used_at is None and entry.clearance.expires_at <= now)
            ):
                continue
            # A conversion is never modified: every clearance of its intent has its terms.
            per_intent[entry.clearance.intent_id] = (entry.coid, leg, terms)
        fills = repo.fills_by_coid(store, [coid for coid, _, _ in per_intent.values()])
        total = ZERO
        for coid, leg, terms in per_intent.values():
            left = max(ZERO, (terms.cash_quantity or ZERO) - leg.spent(fills.get(coid, [])))
            total += leg.received(left, terms.price.limit)
        return total

    def _currency_cap(
        self, leg: ConversionLeg, amount: Decimal, incoming: Decimal, found: _Found
    ) -> list[PretradeFailure]:
        """lite's single-foreign-currency cap on the currency a conversion buys: its cash, what
        the conversion and the other conversions working into it bring in and the positions
        quoted in it, valued at the pair's reference rate, against the NAV."""
        target = leg.target
        cash, positions, quote = found.cash, found.positions, found.quote
        if target == self._base or cash is None or positions is None or quote is None:
            return []
        reference = quote.evidence.reference()
        if cash.nav is None or cash.nav <= 0:
            return [
                PretradeFailure(
                    Reason.NAV_UNAVAILABLE,
                    f"the NAV is not readable in the base currency ({cash.nav_refusal})",
                )
            ]
        if reference is None:  # pragma: no cover - the collar refused a quote without one
            return [PretradeFailure(Reason.QUOTE_POLICY, "the pair's quote has no reference")]
        # One side of every pair is the base currency (the schema checks it), so the pair's own
        # rate values the other.
        to_base = reference if target == leg.pair.base else 1 / reference
        unreadable = [
            str(p.conid)
            for p in positions.positions
            if p.currency == target and p.quantity != 0 and p.market_value is None
        ]
        if unreadable:
            return [
                PretradeFailure(
                    Reason.EXPOSURE_UNREADABLE,
                    f"positions in {target} without a market value ({', '.join(unreadable)}): "
                    "the currency's exposure cannot be measured",
                )
            ]
        held = (line.cash if (line := cash.line(target)) is not None else ZERO) + sum(
            (p.market_value or ZERO for p in positions.positions if p.currency == target), ZERO
        )
        exposure = (held + incoming + leg.received(amount, reference)) * to_base
        cap = cash.nav * self._max_currency
        if exposure > cap:
            return [
                PretradeFailure(
                    Reason.CURRENCY_CAP,
                    f"{target} would be {exposure:.2f} {self._base} of exposure, above the "
                    f"single-foreign-currency cap of {cap:.2f} ({self._max_currency} of NAV)",
                )
            ]
        return []

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
                # A buy spends the cash of its currency; a conversion that of the currency it
                # converts from, which its clearance names.
                or not (clearance.terms.side is OrderSide.BUY or clearance.terms.conversion)
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
            leg = conversion_leg(self._pairs, terms) if terms.conversion else None
            if terms.quantity is not None:
                filled = sum((fill.quantity for fill in done), ZERO)
                total += amount * max(ZERO, 1 - filled / terms.quantity)
            elif leg is not None:
                total += max(ZERO, amount - leg.spent(done))
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
            if clearance.effect is not RiskEffect.ADDS or clearance.terms.conversion:
                continue  # a conversion funds a trade the caps already count
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
        if found.leg is not None:
            asset_class, currency = AssetClass.FX, found.leg.source
        else:
            asset_class, currency = found.traded.asset_class, found.traded.currency
        rule = self._quotes.rule_for(asset_class)
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
            asset_class=asset_class,
            quote=quote.evidence,
            underlying_quote=None
            if found.underlying_quote is None
            else found.underlying_quote.evidence,
            sigma=quote.sigma,
            issued_at=now,
            expires_at=expires,
            currency=currency,
            notional=notional,
            notional_base=notional_base,
        )
        repo.record_clearance(store, clearance)
        return clearance
