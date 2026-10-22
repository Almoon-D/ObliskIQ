"""The instrument: one contract the system can read, and perhaps buy.

:class:`Instrument` is the universe's record of a contract (Session 2's store). An order trades a
:class:`TradedInstrument`: what the pre-trade gate must know about the contract it sends, the
listing venue whose calendar governs it, the description the leveraged and inverse markers are
matched against and, for an option, its underlying and expiry.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Iterable
from datetime import date
from decimal import Decimal
from typing import Annotated, Self

from pydantic import AfterValidator, Field, PositiveInt, model_validator

from .base import Conid, CountryCode, CurrencyCode, DomainModel, NonEmptyText, UtcDatetime
from .enums import AssetClass, InstrumentRole, KidStatus, ShareClass


def _positive(value: Decimal) -> Decimal:
    if not value.is_finite() or value <= 0:
        raise ValueError("must be a positive, finite amount")
    return value


class Instrument(DomainModel):
    """A contract pinned by IBKR contract id, listing venue and currency.

    Unknown attributes are ``None``, never a default: an unknown ``ucits`` is not ``False``
    and an unknown ``lot_size`` is not 1. ``history_exchange`` ``None`` is an instruction (send
    no exchange with a history request), not a gap. A correction supersedes, it never deletes:
    the old row keeps its history and names its replacement.
    """

    conid: Conid
    symbol: NonEmptyText
    listing_exchange: NonEmptyText
    currency: CurrencyCode
    asset_class: AssetClass
    domicile: CountryCode | None
    ucits: bool | None
    kid_status: KidStatus | None
    share_class: ShareClass | None
    lot_size: PositiveInt | None
    option_exchange: NonEmptyText | None
    history_exchange: NonEmptyText | None
    roles: frozenset[InstrumentRole] = Field(min_length=1)
    superseded_by: Conid | None
    created_at: UtcDatetime
    superseded_at: UtcDatetime | None

    @model_validator(mode="after")
    def _supersession_is_coherent(self) -> Self:
        if (self.superseded_by is None) != (self.superseded_at is None):
            raise ValueError("superseded_by and superseded_at are set together or not at all")
        if self.superseded_by == self.conid:
            raise ValueError("an instrument cannot supersede itself")
        if self.superseded_at is not None and self.superseded_at < self.created_at:
            raise ValueError("an instrument cannot be superseded before it was created")
        return self


class TradedInstrument(DomainModel):
    """A contract an order may trade, as the pre-trade gate needs it.

    ``listing_exchange`` is IBKR's code for the venue whose calendar governs the contract (an
    option's own exchange, for an option). ``multiplier`` turns a price into what one unit costs
    (1 for a share, 100 for a US equity option). An option names its underlying and expiry, and
    only an option does.
    """

    conid: Conid
    symbol: NonEmptyText
    listing_exchange: NonEmptyText
    currency: CurrencyCode
    asset_class: AssetClass
    description: NonEmptyText
    multiplier: Annotated[Decimal, AfterValidator(_positive)]
    underlying_conid: Conid | None
    expiry: date | None

    @model_validator(mode="after")
    def _option_fields(self) -> Self:
        named = (self.underlying_conid is not None, self.expiry is not None)
        expected = (True, True) if self.asset_class is AssetClass.OPTION else (False, False)
        if named != expected:
            raise ValueError("an option, and only an option, names its underlying and expiry")
        if self.underlying_conid == self.conid:
            raise ValueError("an option cannot be its own underlying")
        return self


def leveraged_inverse_marker(description: str, markers: Iterable[str]) -> str | None:
    """The first of ``markers`` found in ``description``, ignoring case; ``None`` when none is.

    Leveraged and inverse products are recognised by their resolved description, never their
    ticker (``BULL`` is a substring of plenty of innocent names).
    """
    folded = description.casefold()
    return next((marker for marker in markers if marker.casefold() in folded), None)
