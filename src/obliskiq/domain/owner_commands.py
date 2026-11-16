"""What the owner asks for in a message: a deposit, a dated withdrawal, a setting change or an
answer to a question (MASTER_PLAN §5.10 CommandParser, §5.15). A command is always echoed back
for confirmation before anything is recorded; none carries an order or a size.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from datetime import date
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import Field

from .base import CurrencyCode, DomainModel, NonEmptyText

type PositiveAmount = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]


class DepositCommand(DomainModel):
    """A deposit: "I added 1.237,57 € to your cash"."""

    kind: Literal["deposit"] = "deposit"
    amount: PositiveAmount
    currency: CurrencyCode


class WithdrawalCommand(DomainModel):
    """A dated withdrawal: "in 15 days I need 1.000 €", due by ``due``."""

    kind: Literal["withdrawal"] = "withdrawal"
    amount: PositiveAmount
    currency: CurrencyCode
    due: date


class SettingCommand(DomainModel):
    """A setting change: "set thesis.caps.position 0.20" (checked like ``/set``)."""

    kind: Literal["setting"] = "setting"
    key: NonEmptyText
    value: NonEmptyText


class AnswerCommand(DomainModel):
    """An answer to an owner question: "Q3 postpone"."""

    kind: Literal["answer"] = "answer"
    question_id: Annotated[int, Field(gt=0)]
    option: NonEmptyText


type OwnerCommand = DepositCommand | WithdrawalCommand | SettingCommand | AnswerCommand
