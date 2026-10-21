"""Capital and cash flows: the owner's deposits, withdrawals and their reservations, the
adjustments that explain a broker cash change, the questions the bot asks before it cuts what it
should not, and the holdings the owner keeps elsewhere (MASTER_PLAN §5.15, §5.11).

Amounts are ``Decimal`` in the currency each record names. A deposit, a withdrawal and a
reservation carry a positive amount; an adjustment carries a signed one (money in is positive).
Dates are the day a flow takes effect; stamps are UTC.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from .base import CountryCode, CurrencyCode, DomainModel, NonEmptyText, UtcDatetime

type Money = Annotated[Decimal, Field(allow_inf_nan=False)]
type PositiveMoney = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
type RecordId = Annotated[int, Field(ge=1)]


class FlowKind(StrEnum):
    """What a cash-flow record is."""

    DEPOSIT = "deposit"
    WITHDRAWAL = "withdrawal"
    ADJUSTMENT = "adjustment"


class AdjustmentKind(StrEnum):
    """What explains a cash change the bot's fills and the owner's flows do not."""

    DIVIDEND = "dividend"
    INTEREST = "interest"
    FEE = "fee"
    WITHHOLDING_TAX = "withholding_tax"
    CORRECTION = "correction"
    OTHER = "other"


class _Flow(DomainModel):
    """What every cash-flow record shares.

    ``on`` is the day the flow takes effect. ``matched_at`` is when the broker's cash showed it
    (``None``: not yet, or never, as in a paper account, whose cash an owner's flow never reaches).
    ``in_book`` says whether it counts toward the managed book under ``capital.mode: cap``.
    """

    flow_id: RecordId | None = None
    currency: CurrencyCode
    on: date
    recorded_at: UtcDatetime
    recorded_by: NonEmptyText
    matched_at: UtcDatetime | None = None
    cancelled_at: UtcDatetime | None = None
    in_book: bool = True
    note: NonEmptyText | None = None

    @property
    def live(self) -> bool:
        """Whether the record still counts: it was not cancelled."""
        return self.cancelled_at is None


class Deposit(_Flow):
    """Money the owner added to the account: it raises managed capital and is deployed toward
    the targets, optionally in tranches."""

    kind: Literal[FlowKind.DEPOSIT] = FlowKind.DEPOSIT
    amount: PositiveMoney

    @property
    def signed(self) -> Decimal:
        return self.amount


class Withdrawal(_Flow):
    """Money the owner took out, usually the end of a reservation."""

    kind: Literal[FlowKind.WITHDRAWAL] = FlowKind.WITHDRAWAL
    amount: PositiveMoney
    reservation_id: RecordId | None = None

    @property
    def signed(self) -> Decimal:
        return -self.amount


class Adjustment(_Flow):
    """A broker cash change classified by the owner: a dividend, interest, a fee and so on."""

    kind: Literal[FlowKind.ADJUSTMENT] = FlowKind.ADJUSTMENT
    category: AdjustmentKind
    amount: Money
    #: The unexplained cash event it classifies, if any.
    event_id: RecordId | None = None

    @model_validator(mode="after")
    def _not_zero(self) -> Self:
        if self.amount == 0:
            raise ValueError("an adjustment moves some money")
        return self

    @property
    def signed(self) -> Decimal:
        return self.amount


type CashFlow = Annotated[Deposit | Withdrawal | Adjustment, Field(discriminator="kind")]


class ReservationState(StrEnum):
    """Where a withdrawal reservation stands."""

    OPEN = "open"  # planned for: investable capital is reduced by it
    WITHDRAWN = "withdrawn"  # the owner took the money
    CANCELLED = "cancelled"  # the owner no longer needs it


class WithdrawalReservation(DomainModel):
    """Money the owner will take out by ``due_date``. Investable capital drops by it at once, and
    the planner raises the cash before ``due_date`` less settlement and a buffer."""

    reservation_id: RecordId | None = None
    amount: PositiveMoney
    currency: CurrencyCode
    due_date: date
    reason: NonEmptyText
    state: ReservationState = ReservationState.OPEN
    created_at: UtcDatetime
    created_by: NonEmptyText
    updated_at: UtcDatetime

    @model_validator(mode="after")
    def _due_after_creation(self) -> Self:
        if self.due_date < self.created_at.date():
            raise ValueError("a reservation is due on or after the day it is made")
        return self


class ReservationEvent(DomainModel):
    """One audited change of a reservation: made, postponed, reduced, withdrawn, cancelled."""

    reservation_id: RecordId
    seq: Annotated[int, Field(ge=1)]
    at: UtcDatetime
    actor: NonEmptyText
    detail: NonEmptyText


# ── owner questions ──


class QuestionKind(StrEnum):
    """Why the bot asks."""

    #: Meeting a reservation would cut protected positions (an entry window, a probe, a catalyst).
    PROTECTED_CUT = "protected_cut"
    #: A sale expected to do well is held to the last safe days; a later date is offered.
    LATER_DATE = "later_date"


class QuestionState(StrEnum):
    OPEN = "open"
    ANSWERED = "answered"
    #: Not answered by its deadline: the default option was applied (the withdrawal wins).
    EXPIRED = "expired"


class OptionEffect(StrEnum):
    """What choosing an option does."""

    PROCEED = "proceed"  # sell what the plan needs, protected positions included
    POSTPONE = "postpone"  # move the reservation's due date to ``due_date``
    REDUCE = "reduce"  # reduce the reservation to ``amount``
    AFTER_YEAR_END = "after_year_end"  # sell no earlier than ``not_before`` (a new tax year)


class QuestionOption(DomainModel):
    """One answer the owner may give, with what it costs. Costs are in the base currency;
    ``None`` when they cannot be estimated (no lots, no price), never a guess."""

    key: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$")]
    effect: OptionEffect
    label: NonEmptyText
    tax_cost: Money | None
    strategy_cost: Money | None
    due_date: date | None = None
    amount: PositiveMoney | None = None
    not_before: date | None = None

    @model_validator(mode="after")
    def _effect_fields(self) -> Self:
        wanted = {
            OptionEffect.PROCEED: (False, False, False),
            OptionEffect.POSTPONE: (True, False, False),
            OptionEffect.REDUCE: (False, True, False),
            OptionEffect.AFTER_YEAR_END: (False, False, True),
        }[self.effect]
        given = (self.due_date is not None, self.amount is not None, self.not_before is not None)
        if given != wanted:
            raise ValueError(f"a {self.effect.value} option names exactly what it changes")
        return self


class OwnerQuestion(DomainModel):
    """A decision the bot needs from the owner, with its options and a deadline.

    Without an answer by ``deadline`` the ``default`` option is applied and the question is
    ``expired``; the default is always the one under which the withdrawal is met on time.
    """

    question_id: RecordId | None = None
    kind: QuestionKind
    reservation_id: RecordId
    context: NonEmptyText
    options: tuple[QuestionOption, ...] = Field(min_length=2)
    default: NonEmptyText
    deadline: UtcDatetime
    state: QuestionState = QuestionState.OPEN
    answer: NonEmptyText | None = None
    answered_at: UtcDatetime | None = None
    answered_by: NonEmptyText | None = None
    created_at: UtcDatetime

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        keys = [option.key for option in self.options]
        if len(keys) != len(set(keys)):
            raise ValueError("each option has its own key")
        if self.default not in keys:
            raise ValueError("the default is one of the options")
        if (self.state is QuestionState.OPEN) != (self.answer is None):
            raise ValueError("an open question has no answer; a closed one has one")
        if self.answer is not None and self.answer not in keys:
            raise ValueError("the answer is one of the options")
        if (self.answered_at is None) != (self.answer is None):
            raise ValueError("an answer is stamped")
        return self

    def option(self, key: str) -> QuestionOption | None:
        return next((option for option in self.options if option.key == key), None)

    @property
    def chosen(self) -> QuestionOption | None:
        """The option in force: the answer, or nothing while the question is open."""
        return None if self.answer is None else self.option(self.answer)


# ── unexplained cash ──


class CashEventState(StrEnum):
    PENDING = "pending"  # waiting for the owner's classification; new risk is frozen
    CLASSIFIED = "classified"


class CashEvent(DomainModel):
    """A change in the broker's cash that neither the bot's fills nor a recorded flow explains.

    ``amount`` is the unexplained difference in ``currency`` (positive: more cash than expected)
    as the latest reconciliation measured it.
    """

    event_id: RecordId | None = None
    currency: CurrencyCode
    amount: Money
    detected_at: UtcDatetime
    updated_at: UtcDatetime
    state: CashEventState = CashEventState.PENDING
    classified_at: UtcDatetime | None = None
    classified_by: NonEmptyText | None = None

    @model_validator(mode="after")
    def _classified(self) -> Self:
        done = self.state is CashEventState.CLASSIFIED
        if done != (self.classified_at is not None) or done != (self.classified_by is not None):
            raise ValueError("a classified event says when and by whom")
        return self


# ── holdings elsewhere ──


class ExternalHolding(DomainModel):
    """A holding at a broker with no readable API, entered by hand (lite's external_positions).

    ``last_updated`` is when a person last confirmed the figure; staleness is measured from it.
    """

    holding_id: RecordId | None = None
    broker: NonEmptyText
    symbol: NonEmptyText
    isin: NonEmptyText | None = None
    quantity: Annotated[Decimal, Field(allow_inf_nan=False)]
    currency: CurrencyCode
    market_value_base: Annotated[Decimal, Field(allow_inf_nan=False)]
    last_updated: date
    notes: NonEmptyText | None = None
    sector: NonEmptyText | None = None
    country: CountryCode | None = None
