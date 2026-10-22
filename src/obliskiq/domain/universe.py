"""The universe's records in the store: ingestion's refusals, issuers' holdings files and their
lines, market-scanner rows and stock candidates' resolutions.

A holdings line keeps the issuer's own words (sector, location, exchange) beside what this system
maps them to, and ``None`` where the map has no entry: an unmapped exchange is not guessed.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from datetime import date
from enum import StrEnum
from typing import Self

from pydantic import NonNegativeInt, model_validator

from .base import Conid, CountryCode, CurrencyCode, DomainModel, NonEmptyText, UtcDatetime


class RejectionOrigin(StrEnum):
    """Where a refused instrument came from."""

    DECLARED = "declared"
    HOLDINGS = "holdings"
    SCANNER = "scanner"


class Rejection(DomainModel):
    """An instrument refused at ingestion, and why (a leveraged or inverse product)."""

    rejected_at: UtcDatetime
    symbol: NonEmptyText
    origin: RejectionOrigin
    listing_exchange: NonEmptyText | None
    conid: Conid | None
    description: NonEmptyText | None
    reason: NonEmptyText


class HoldingsFile(DomainModel):
    """One issuer holdings file as downloaded; ``as_of`` is the date it says it is of."""

    fund_symbol: NonEmptyText
    as_of: date
    issuer: NonEmptyText
    url: NonEmptyText
    downloaded_at: UtcDatetime
    content_sha256: NonEmptyText
    rows: NonNegativeInt


class Holding(DomainModel):
    """One line of a holdings file. ``weight`` is a fraction of the fund (0.05 is 5 %);
    ``candidate_key`` groups the listings of one company, and only a share has one."""

    fund_symbol: NonEmptyText
    as_of: date
    line: NonNegativeInt
    ticker: NonEmptyText
    name: NonEmptyText
    asset_class: NonEmptyText
    issuer_sector: NonEmptyText | None
    sector: NonEmptyText | None
    location: NonEmptyText | None
    country: CountryCode | None
    exchange: NonEmptyText | None
    ibkr_exchange: NonEmptyText | None
    currency: CurrencyCode | None
    weight: float | None
    candidate_key: NonEmptyText | None


class ScannerRow(DomainModel):
    """One contract an IBKR market-scanner run returned, for ``market``."""

    scanned_on: date
    location: NonEmptyText
    scan_type: NonEmptyText
    rank: NonNegativeInt
    market: NonEmptyText
    conid: Conid
    symbol: NonEmptyText | None
    listing_exchange: NonEmptyText | None
    company_name: NonEmptyText | None
    candidate_key: NonEmptyText
    scanned_at: UtcDatetime


class CandidateResolution(DomainModel):
    """A stock candidate resolved through IBKR's contract search: its conid, or why none."""

    candidate_key: NonEmptyText
    resolved_at: UtcDatetime
    conid: Conid | None
    symbol: NonEmptyText | None
    listing_exchange: NonEmptyText | None
    currency: CurrencyCode | None
    pinned: bool
    pinned_for: NonEmptyText | None
    failure: NonEmptyText | None

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if (self.conid is None) != (self.failure is not None):
            raise ValueError("a resolution names its conid or its failure, not both")
        if self.pinned and self.conid is None:
            raise ValueError("only a resolved candidate can be pinned")
        return self
