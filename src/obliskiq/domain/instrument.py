"""The instrument: one contract the system can read, and perhaps buy.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from typing import Self

from pydantic import Field, PositiveInt, model_validator

from .base import Conid, CountryCode, CurrencyCode, DomainModel, NonEmptyText, UtcDatetime
from .enums import AssetClass, InstrumentRole, KidStatus, ShareClass


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
