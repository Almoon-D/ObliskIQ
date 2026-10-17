"""What the tax engine holds: the identity of a security, its lots, realised gains and losses,
income and the summary of a tax year (MASTER_PLAN §5.16).

Amounts are ``Decimal`` in the account's base currency. A loss is a negative gain; an amount
deferred or released under a wash-sale rule is a non-negative magnitude. The rules that move
these values are a jurisdiction's :class:`~obliskiq.jurisdiction.tax.TaxPolicy`; the mechanics
that apply them are the ``tax`` package.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import re
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, Field, model_validator

from .base import CountryCode, DomainModel, NonEmptyText

_ISIN_RE = re.compile(r"[A-Z]{2}[A-Z0-9]{9}[0-9]")


def _isin(value: str) -> str:
    if not _ISIN_RE.fullmatch(value):
        raise ValueError("must be a twelve-character ISIN, such as IE00B4L5Y983")
    return value


type Isin = Annotated[str, AfterValidator(_isin)]
#: Amounts: a domain model refuses NaN and the infinities, so each is finite.
type Money = Decimal
type Magnitude = Annotated[Decimal, Field(ge=0)]
type Quantity = Annotated[Decimal, Field(gt=0)]


class SecurityKind(StrEnum):
    """What a security is, as far as the tax rules care."""

    SHARE = "share"
    ETF = "etf"
    FUND = "fund"
    BOND = "bond"
    OPTION = "option"


#: Collective investments: their homogeneity follows the issuer and the index they track.
COLLECTIVE_KINDS = frozenset({SecurityKind.ETF, SecurityKind.FUND})


class SecurityIdentity(DomainModel):
    """What the tax rules need to know about one security.

    ``key`` is how the holder names it (a conid as text in the live path, a series key in
    research). ``listed`` is whether it is admitted to an official secondary market, which
    decides the wash-sale window. A collective investment names its issuer and the index it
    tracks: homogeneity cannot be judged without them.
    """

    key: NonEmptyText
    kind: SecurityKind
    isin: Isin | None
    issuer: NonEmptyText | None
    tracked_index: NonEmptyText | None
    listed: bool

    @model_validator(mode="after")
    def _collective_names_issuer_and_index(self) -> Self:
        if self.kind in COLLECTIVE_KINDS and (self.issuer is None or self.tracked_index is None):
            raise ValueError("an ETF or a fund names its issuer and the index it tracks")
        return self


class TaxLot(DomainModel):
    """Units of one security bought together, with what they cost in the base currency.

    ``cost`` includes the costs and taxes of the acquisition. ``deferred_loss`` is a loss the
    wash-sale rule attached to these units, released as they are sold. ``replacement_units``
    counts the units already matched against an earlier loss, so no unit defers two losses.
    """

    lot_id: NonEmptyText
    security: NonEmptyText
    acquired_on: date
    quantity: Quantity
    cost: Magnitude
    deferred_loss: Magnitude
    replacement_units: Magnitude

    @model_validator(mode="after")
    def _replacement_within_quantity(self) -> Self:
        if self.replacement_units > self.quantity:
            raise ValueError("replacement_units cannot exceed the lot's quantity")
        return self


class Realisation(DomainModel):
    """The result of selling part or all of one lot.

    ``gain`` is proceeds minus cost (negative for a loss). ``released`` is the deferred loss the
    sold units carried; ``deferred`` is the part of this result the wash-sale rule set aside.
    ``counted`` is what enters the year's gains: ``gain - released + deferred``.
    """

    security: NonEmptyText
    lot_id: NonEmptyText
    acquired_on: date
    disposed_on: date
    quantity: Quantity
    proceeds: Magnitude
    cost: Magnitude
    released: Magnitude
    deferred: Magnitude

    @property
    def gain(self) -> Decimal:
        return self.proceeds - self.cost

    @property
    def counted(self) -> Decimal:
        return self.gain - self.released + self.deferred


class IncomeEvent(DomainModel):
    """Income received on a security: the gross amount and what the source withheld."""

    security: NonEmptyText
    received_on: date
    gross: Magnitude
    withheld: Magnitude
    source_country: CountryCode | None

    @model_validator(mode="after")
    def _withheld_within_gross(self) -> Self:
        if self.withheld > self.gross:
            raise ValueError("the amount withheld cannot exceed the gross income")
        return self


class ProposedTrade(DomainModel):
    """A trade the tax hooks are asked about: ``price`` per unit in the base currency."""

    security: NonEmptyText
    side: Literal["buy", "sell"]
    quantity: Quantity
    price: Magnitude
    on: date


type Component = Literal["income", "gains"]


class CarryForward(DomainModel):
    """A negative balance of one component of the savings base, kept for later years."""

    component: Component
    arising_year: int
    amount: Magnitude


class TaxYearSummary(DomainModel):
    """One settled tax year: the components, the offsets applied, the tax and when it is paid.

    ``income`` and ``gains`` are the year's own net components before any offset; ``base`` is
    the taxable base after offsets and carry-forwards. ``rate_table_covers_year`` is false when
    the profile's rate table was verified for other years only: the tax is then an estimate
    under the rates it does hold.
    """

    tax_year: int
    income: Money
    gains: Money
    base: Magnitude
    tax_before_credit: Magnitude
    foreign_tax_credit: Magnitude
    tax_due: Magnitude
    carried_in_used: Magnitude
    carried_out: tuple[CarryForward, ...]
    expired: Magnitude
    rate_table_covers_year: bool
    payment_date: date
