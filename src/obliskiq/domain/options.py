"""The options sleeve's records: its positions, the events of its penalty ledger, its zombie
readings and the would-be entries it logs in shadow (MASTER_PLAN §5.6).

Every amount is in the base currency, which is what the sleeve's pool and its penalty balance are
measured in; a figure converted from the contract's currency carries the conversion it took
(:class:`Conversion`), so it can be checked against the rate that produced it.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Self
from uuid import UUID

from pydantic import Field, PositiveFloat, PositiveInt, model_validator

from .base import Conid, CurrencyCode, DomainModel, NonEmptyText, UtcDatetime
from .enums import MarketDataStatus, OptionRight

#: A positive money amount, finite.
type Money = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
#: A money amount at or above zero, finite.
type NonNegativeMoney = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
type Probability = Annotated[float, Field(ge=0, le=1)]


class Conversion(DomainModel):
    """How a figure in the contract's currency became the base-currency amount: the currency,
    the amount in it, and the multiplier taken (base = quote_amount x fx_to_base)."""

    currency: CurrencyCode
    quote_amount: NonNegativeMoney
    fx_to_base: Money


class OptionPosition(DomainModel):
    """One long-option position of the sleeve, bought in one block.

    ``premium_base`` is the whole premium paid, in the base currency. ``close_by`` is the session
    it is sold by (the last tradable session less the mandatory-close buffer). ``position_id`` is
    ``None`` until the store numbers it.
    """

    position_id: PositiveInt | None
    underlying_conid: Conid
    option_conid: Conid | None
    expiry: date
    strike: PositiveFloat
    right: OptionRight
    contracts: PositiveInt
    multiplier: PositiveFloat
    premium_per_unit: Money
    currency: CurrencyCode
    premium_base: Money
    fx_to_base: Money | None
    opened_on: date
    close_by: date
    target: PositiveFloat | None
    prob_target_at_entry: Probability | None
    catalyst_date: date | None
    intent_id: UUID | None
    closed_on: date | None = None
    exit_reason: NonEmptyText | None = None

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if (self.closed_on is None) != (self.exit_reason is None):
            raise ValueError("a position closes with the reason it closed for, and only then")
        if self.closed_on is not None and self.closed_on < self.opened_on:
            raise ValueError("a position cannot close before it opened")
        if not self.opened_on <= self.close_by <= self.expiry:
            raise ValueError("close_by falls between the opening and the expiry")
        return self

    @property
    def is_call(self) -> bool:
        return self.right is OptionRight.CALL

    @property
    def is_open(self) -> bool:
        return self.closed_on is None


class LedgerEventType(StrEnum):
    """What moved the sleeve's pool or its penalty balance."""

    #: Premium tied up by an entry, released by the position closing.
    PREMIUM_COMMITTED = "premium_committed"
    #: The penalty booked when a loss is detected (a confirmed zombie, an invalidation).
    LOSS_DETECTED = "loss_detected"
    #: The realised loss at the close, reconciled against what detection booked.
    LOSS_CLOSED = "loss_closed"
    #: A realised gain on a non-option position exited for a qualifying reason.
    QUALIFYING_GAIN = "qualifying_gain"
    #: A win inside the sleeve, recorded; it does not reduce the penalty.
    OPTION_GAIN = "option_gain"


class LedgerEvent(DomainModel):
    """One event of the penalty ledger. ``amount`` is its magnitude; ``penalty_added`` its signed
    move of the balance; ``penalty_balance_after`` the running balance (the audit trail)."""

    event_id: PositiveInt | None
    event_on: date
    event_type: LedgerEventType
    position_id: PositiveInt | None
    conid: Conid | None
    amount: NonNegativeMoney
    penalty_added: Annotated[Decimal, Field(allow_inf_nan=False)]
    penalty_balance_after: NonNegativeMoney
    exit_reason: NonEmptyText | None
    conversion: Conversion | None
    note: NonEmptyText
    recorded_at: UtcDatetime

    @model_validator(mode="after")
    def _shape(self) -> Self:
        qualifying = self.event_type is LedgerEventType.QUALIFYING_GAIN
        if qualifying and (
            self.conid is None or self.exit_reason is None or self.position_id is not None
        ):
            raise ValueError("a qualifying gain names its instrument and exit reason, no position")
        if not qualifying and self.position_id is None:
            raise ValueError(f"a {self.event_type.value} event belongs to a position")
        return self


class ZombieObservation(DomainModel):
    """One review of an open position against the zombie test."""

    position_id: PositiveInt
    observed_on: date
    all_conditions_met: bool
    delta: float | None
    dte: int | None
    prob_target: Probability | None
    prob_ratio_vs_entry: float | None
    recoverable_pct_of_premium: float | None
    detail: dict[str, dict[str, float | bool | None]]
    recorded_at: UtcDatetime


class ShadowEntry(DomainModel):
    """A contract the sleeve would have entered on ``as_of``, had its activation condition held.

    It failed no candidate gate; ``untested`` names the gates it could not be put through, and
    ``activation`` why the sleeve did not trade.
    """

    as_of: date
    underlying_conid: Conid
    expiry: date
    strike: PositiveFloat
    right: OptionRight
    snapshot_date: date
    option_conid: Conid | None
    bid: Annotated[float, Field(ge=0)] | None
    ask: Annotated[float, Field(ge=0)] | None
    mid: PositiveFloat
    iv: Annotated[float, Field(ge=0)] | None
    delta: Annotated[float, Field(ge=-1, le=1)] | None
    multiplier: PositiveFloat | None
    currency: CurrencyCode
    premium_base: Money | None
    iv_percentile: Probability | None
    market_data_status: MarketDataStatus | None
    untested: tuple[NonEmptyText, ...]
    activation: NonEmptyText
    computed_at: UtcDatetime

    @model_validator(mode="after")
    def _dated(self) -> Self:
        if self.snapshot_date > self.as_of:
            raise ValueError("a shadow entry is priced off a chain collected by its session")
        return self
