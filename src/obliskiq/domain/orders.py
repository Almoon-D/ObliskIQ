"""Orders: intents, the terms an order carries, the clearance that unlocks a write, and what the
broker reports back.

An :class:`OrderIntent` is what the decision layer wants done; its :class:`OrderTerms` are what
would reach the broker. Every price policy carries a limit, so no plain market order can be
expressed: a patient limit, an IBKR algo with a limit cap, or a stop-limit. A
:class:`PretradeClearance` is bound to one intent, one action and the exact terms (by their
digest); the pre-trade gate is its only issuer and the order client spends it once.

Money is ``Decimal``. A role is a label and never a permission: what an order does to risk is
decided by its effect (MASTER_PLAN §5.12; DECISIONS D44).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import hashlib
import json
from collections.abc import Mapping
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Final, Literal, Self
from uuid import UUID

from pydantic import AfterValidator, Field, model_validator

from .base import Conid, CurrencyCode, DomainModel, NonEmptyText, UtcDatetime
from .enums import AssetClass, MarketDataStatus

#: The uuid version an intent id carries (time-ordered, ``uuid.uuid7()``).
INTENT_UUID_VERSION = 7

#: What the holdings registry calls a position the bot did not open.
UNMANAGED: Final = "unmanaged"


def _finite_positive(value: Decimal) -> Decimal:
    if not value.is_finite() or value <= 0:
        raise ValueError("must be a positive, finite amount")
    return value


def _uuid7(value: UUID) -> UUID:
    if value.version != INTENT_UUID_VERSION:
        raise ValueError("must be a version 7 uuid (uuid.uuid7())")
    return value


type PositiveAmount = Annotated[Decimal, AfterValidator(_finite_positive)]
type Amount = Annotated[Decimal, Field(allow_inf_nan=False)]
type IntentId = Annotated[UUID, AfterValidator(_uuid7)]


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class IntentRole(StrEnum):
    """What an intent is for. A label: it never decides what the order may do."""

    ENTRY = "entry"
    PROTECTIVE = "protective"
    EXIT = "exit"
    CONVERSION = "conversion"


class IntentState(StrEnum):
    """The life of an intent (``execution/state.py`` holds the legal transitions)."""

    PROPOSED = "proposed"
    APPROVED = "approved"
    STAGED = "staged"
    SUBMITTED = "submitted"
    WORKING = "working"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"


#: States an intent never leaves.
TERMINAL_STATES: Final = frozenset(
    {IntentState.FILLED, IntentState.CANCELLED, IntentState.REJECTED, IntentState.EXPIRED}
)


class ExecutionMode(StrEnum):
    """Where orders go (MASTER_PLAN §5.19), in ascending order of what they risk."""

    SHADOW = "shadow"
    PAPER = "paper"
    LIVE_SMALL = "live-small"
    LIVE = "live"

    @property
    def rank(self) -> int:
        return list(ExecutionMode).index(self)


class Sleeve(StrEnum):
    """The part of the book an intent or a holding belongs to."""

    CORE = "core"
    SATELLITE = "satellite"
    OPTIONS = "options"


type HoldingOwner = Sleeve | Literal["unmanaged"]


class TimeInForce(StrEnum):
    """Times in force the order path can send. ``OPG`` is absent: orders avoid the auctions."""

    DAY = "DAY"
    GTC = "GTC"
    IOC = "IOC"


class RiskEffect(StrEnum):
    """What an order does to risk. Only a sell of a managed long position, within it, reduces it."""

    REDUCES = "reduces_risk"
    ADDS = "adds_risk"


class ClearanceAction(StrEnum):
    PLACE = "place"
    MODIFY = "modify"


# ── price policies: every one carries a limit ──


class LimitPrice(DomainModel):
    """A patient limit."""

    kind: Literal["limit"] = "limit"
    limit: PositiveAmount


class StopLimitPrice(DomainModel):
    """A stop that becomes a limit once triggered (the protective exits)."""

    kind: Literal["stop_limit"] = "stop_limit"
    stop: PositiveAmount
    limit: PositiveAmount


class AlgoParameter(DomainModel):
    name: NonEmptyText
    value: NonEmptyText


class AlgoLimitPrice(DomainModel):
    """An IBKR algo with a limit cap: the algo works the order, never past ``limit``."""

    kind: Literal["algo"] = "algo"
    strategy: NonEmptyText
    limit: PositiveAmount
    parameters: tuple[AlgoParameter, ...] = ()


type PricePolicy = Annotated[
    LimitPrice | StopLimitPrice | AlgoLimitPrice, Field(discriminator="kind")
]


def _canonical(value: object) -> object:
    """A form of ``value`` whose JSON text is the same for equal terms (``1.50`` is ``1.5``)."""
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, Mapping):
        return {str(key): _canonical(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_canonical(item) for item in value]
    if isinstance(value, StrEnum):
        return value.value
    return value


class OrderTerms(DomainModel):
    """Exactly what an order sends, apart from the account and the customer order id.

    One of ``quantity`` (units) and ``cash_quantity`` (money); a sell, but for a conversion, is
    sized in units, so the no-short check (I2) can count it. A currency conversion
    (``conversion``) is sized in money, sent as IBKR's ``fxQty`` with ``isCcyConv``, and is a
    plain limit.
    """

    conid: Conid
    side: OrderSide
    quantity: PositiveAmount | None = None
    cash_quantity: PositiveAmount | None = None
    price: PricePolicy
    tif: TimeInForce
    venue: NonEmptyText
    conversion: bool = False

    @model_validator(mode="after")
    def _one_size(self) -> Self:
        if (self.quantity is None) == (self.cash_quantity is None):
            raise ValueError("give exactly one of quantity and cash_quantity")
        if self.conversion and (self.cash_quantity is None or self.price.kind != "limit"):
            raise ValueError("a currency conversion is a limit order sized in cash_quantity")
        if self.side is OrderSide.SELL and not self.conversion and self.quantity is None:
            raise ValueError("a sell is sized in units, so the no-short check can count it")
        return self

    def digest(self) -> str:
        """SHA-256 of the canonical terms: equal terms, equal digest; any change, a new one."""
        text = json.dumps(_canonical(self.model_dump()), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(text.encode()).hexdigest()


class ProtectiveSpec(DomainModel):
    """How a position's protective stop rests at the broker (MASTER_PLAN §5.12, DECISIONS D67).

    ``stop`` is the price the stop triggers at when it is first placed (a trailing roll-up raises
    the working order's stop later). The limit sits at most ``limit_offset_fraction`` below the
    stop: an offset set from the instrument's volatility and its overnight-gap distribution, with
    a configured floor (``execution/protective.py``); ``basis`` says how. The pre-trade gate
    checks every resting stop against its intent's spec.
    """

    stop: PositiveAmount
    limit_offset_fraction: Annotated[Decimal, Field(gt=0, lt=1, allow_inf_nan=False)]
    basis: NonEmptyText

    def lowest_limit(self, stop: Decimal) -> Decimal:
        """The lowest limit a stop at ``stop`` may carry under this spec."""
        return stop * (1 - self.limit_offset_fraction)


#: The roles that may carry a protective spec: an entry (the stop its fills get) and a stop.
_SPEC_ROLES = frozenset({IntentRole.ENTRY, IntentRole.PROTECTIVE})


class OrderIntent(DomainModel):
    """What the decision layer wants done, and where it stands."""

    intent_id: IntentId
    sleeve: Sleeve
    thesis_id: NonEmptyText | None = None
    role: IntentRole
    terms: OrderTerms
    reason: NonEmptyText
    mode: ExecutionMode
    approval_required: bool
    created_at: UtcDatetime
    expires_at: UtcDatetime
    parent_id: IntentId | None = None
    protective_id: IntentId | None = None
    #: The protective stop an entry's fills get, or a protective stop's own rule.
    protective: ProtectiveSpec | None = None
    state: IntentState = IntentState.PROPOSED

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if self.expires_at <= self.created_at:
            raise ValueError("an intent must expire after it is created")
        if (self.role is IntentRole.CONVERSION) != self.terms.conversion:
            raise ValueError("the conversion role and conversion terms go together")
        if self.intent_id in (self.parent_id, self.protective_id):
            raise ValueError("an intent cannot link to itself")
        if self.protective is not None and self.role not in _SPEC_ROLES:
            raise ValueError("only an entry or a protective stop carries a protective spec")
        return self

    @property
    def conid(self) -> int:
        return self.terms.conid

    @property
    def side(self) -> OrderSide:
        return self.terms.side


class IntentEvent(DomainModel):
    """One entry of an intent's append-only history: a transition, or a fact within a state
    (``from_state == to_state``: a modify, a partial fill, a reply)."""

    intent_id: IntentId
    seq: Annotated[int, Field(ge=1)]
    at: UtcDatetime
    from_state: IntentState | None
    to_state: IntentState
    detail: NonEmptyText


class QuoteEvidence(DomainModel):
    """The quote a clearance was checked against."""

    conid: Conid
    status: MarketDataStatus | None
    delay_seconds: Annotated[int, Field(ge=0)] | None
    observed_at: UtcDatetime
    bid: PositiveAmount | None = None
    ask: PositiveAmount | None = None
    last: PositiveAmount | None = None

    def reference(self) -> Decimal | None:
        """The mid when both sides are there and not crossed, else the last price."""
        if self.bid is not None and self.ask is not None and self.bid <= self.ask:
            return (self.bid + self.ask) / 2
        return self.last


class PretradeClearance(DomainModel):
    """Permission for one write: one intent, one action (on one broker order for a modify), the
    exact terms, until ``expires_at``, once.

    Only the pre-trade gate (``execution/pretrade.py``) constructs one, and it records it in the
    store; the order client spends it there before sending. A guard test refuses any other
    constructor under ``src/``.
    """

    clearance_id: UUID
    intent_id: IntentId
    action: ClearanceAction
    order_id: NonEmptyText | None = None
    terms: OrderTerms
    effect: RiskEffect
    asset_class: AssetClass
    quote: QuoteEvidence | None
    underlying_quote: QuoteEvidence | None = None
    sigma: Annotated[float, Field(gt=0)] | None = None
    issued_at: UtcDatetime
    expires_at: UtcDatetime
    #: What the order commits at its limit, in ``currency`` (the instrument's), and in the base
    #: currency: the cash check and the daily caps add up the clearances that are still open.
    currency: CurrencyCode | None = None
    notional: PositiveAmount | None = None
    notional_base: PositiveAmount | None = None

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if (self.action is ClearanceAction.MODIFY) != (self.order_id is not None):
            raise ValueError("a modify clearance names the broker order; a placement does not")
        if self.expires_at <= self.issued_at:
            raise ValueError("a clearance must expire after it is issued")
        if self.effect is RiskEffect.REDUCES and (
            self.terms.side is not OrderSide.SELL or self.terms.conversion
        ):
            raise ValueError("only a sell that is not a conversion can reduce risk")
        if self.quote is not None and self.quote.conid != self.terms.conid:
            raise ValueError("the quote must be the order's instrument")
        return self

    @property
    def terms_digest(self) -> str:
        return self.terms.digest()


# ── what the broker reports ──


class BrokerOrder(DomainModel):
    """One order as the broker lists it. Fields IBKR did not send are ``None``."""

    order_id: NonEmptyText
    coid: str | None
    conid: Conid | None
    side: OrderSide | None
    sec_type: str | None
    order_type: str | None
    status: NonEmptyText
    total_quantity: Amount | None
    filled_quantity: Amount | None
    remaining_quantity: Amount | None
    average_price: Amount | None
    tif: str | None
    observed_at: UtcDatetime


class Fill(DomainModel):
    """One execution."""

    execution_id: NonEmptyText
    coid: str | None
    conid: Conid
    side: OrderSide
    quantity: PositiveAmount
    price: PositiveAmount
    commission: Amount | None
    executed_at: UtcDatetime
    simulated: bool = False


class Position(DomainModel):
    """A position as the broker reported it at ``observed_at``; ``market_value`` is IBKR's mark
    in ``currency`` (a fresh read carries it; the store does not keep it)."""

    conid: Conid
    quantity: Amount
    average_cost: Amount | None
    currency: CurrencyCode | None
    observed_at: UtcDatetime
    market_value: Amount | None = None


class CashBalance(DomainModel):
    """Cash in one currency as the broker reported it at ``observed_at``."""

    currency: CurrencyCode
    cash: Amount
    settled_cash: Amount | None
    observed_at: UtcDatetime


class HoldingAssignment(DomainModel):
    """Who a holding belongs to: a sleeve, or ``unmanaged`` (never traded until reassigned)."""

    conid: Conid
    owner: HoldingOwner
    assigned_by: NonEmptyText
    assigned_at: UtcDatetime
    reason: NonEmptyText


class ModeChange(DomainModel):
    """One entry of the mode audit: an owner's upgrade or an automatic downgrade."""

    at: UtcDatetime
    from_mode: ExecutionMode
    to_mode: ExecutionMode
    direction: Literal["upgrade", "downgrade"]
    actor: NonEmptyText
    reason: NonEmptyText

    @model_validator(mode="after")
    def _direction_matches(self) -> Self:
        if self.to_mode is self.from_mode:
            raise ValueError("a mode change changes the mode")
        if (self.to_mode.rank > self.from_mode.rank) != (self.direction == "upgrade"):
            raise ValueError("an upgrade raises the mode and a downgrade lowers it")
        return self


class ApprovalDecision(DomainModel):
    """The owner's word on an intent held in the approval queue."""

    intent_id: IntentId
    approved: bool
    actor: NonEmptyText
    reason: NonEmptyText
    at: UtcDatetime


class StopResizeState(StrEnum):
    """Where a protective stop resized for an exit stands (I5)."""

    RESIZED = "resized"  # the stop was cut so the exit fits beside it
    CLOSED = "closed"  # the exit filled, or the cut was never needed: nothing to restore
    RESTORED = "restored"  # the exit ended unfilled and the stop covers the position again
    RESTORE_FAILED = "restore_failed"  # a restore was refused or failed; retried, alerted


class StopResize(DomainModel):
    """A protective stop cut to make room for an exit, and what restoring it takes.

    ``resized_quantity`` zero means the stop was cancelled (a full exit). ``original_terms`` are
    the stop's terms before the cut; a restore brings it back to them, less what the exit filled.
    """

    resize_id: Annotated[int, Field(ge=1)] | None = None
    exit_intent_id: IntentId
    stop_intent_id: IntentId
    stop_order_id: NonEmptyText
    original_terms: OrderTerms
    resized_quantity: Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
    state: StopResizeState
    created_at: UtcDatetime
    updated_at: UtcDatetime
    detail: NonEmptyText

    @model_validator(mode="after")
    def _a_cut(self) -> Self:
        original = self.original_terms.quantity
        if self.original_terms.side is not OrderSide.SELL or original is None:
            raise ValueError("a protective stop is a sell sized in units")
        if self.resized_quantity >= original:
            raise ValueError("a resize cuts the stop")
        return self


class KillSwitchRecord(DomainModel):
    """One change of the kill switch's database flag."""

    at: UtcDatetime
    on: bool
    actor: NonEmptyText
    reason: NonEmptyText


class ReconcileStatus(StrEnum):
    """How a reconciliation ended. Only ``green`` lets new risk through."""

    GREEN = "green"  # the broker and the store agree
    DRIFT = "drift"  # a finding that freezes new intents
    INCOMPLETE = "incomplete"  # the broker's data was partial or missing: never green


class ReconcileFinding(DomainModel):
    """One thing reconciliation found; ``freezes`` when it stops new risk."""

    kind: NonEmptyText
    detail: NonEmptyText
    freezes: bool


class ReconcileRun(DomainModel):
    """One reconciliation of the broker's orders, trades, positions and cash against the store."""

    at: UtcDatetime
    status: ReconcileStatus
    findings: tuple[ReconcileFinding, ...]

    @model_validator(mode="after")
    def _status_matches(self) -> Self:
        freezing = any(finding.freezes for finding in self.findings)
        if self.status is ReconcileStatus.GREEN and freezing:
            raise ValueError("a run with a freezing finding is not green")
        if self.status is ReconcileStatus.DRIFT and not freezing:
            raise ValueError("a drift names the finding that freezes")
        return self
