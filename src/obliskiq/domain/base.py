"""The base of every domain model and the small validated types they share.

A domain model is frozen, refuses unknown fields and refuses ``nan`` and the infinities: a
value that is not a finite number is absent (``None``), never a measurement. Datetimes are
timezone-aware and normalised to UTC; local times come from ``zoneinfo`` at the edges.

This module imports nothing else of ours, so every package may build on it.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import re
from datetime import UTC, datetime
from typing import Annotated

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field

_CURRENCY_RE = re.compile(r"[A-Z]{3}")
_COUNTRY_RE = re.compile(r"[A-Z]{2}")


class DomainModel(BaseModel):
    """Base for domain models: immutable, no unknown fields, no non-finite numbers."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


def _utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


def _currency_code(value: str) -> str:
    if not _CURRENCY_RE.fullmatch(value):
        raise ValueError("must be a three-letter ISO 4217 currency code in capitals, such as EUR")
    return value


def _country_code(value: str) -> str:
    if not _COUNTRY_RE.fullmatch(value):
        raise ValueError("must be a two-letter ISO 3166 country code in capitals, such as IE")
    return value


def _non_empty(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be empty")
    return value


type UtcDatetime = Annotated[AwareDatetime, AfterValidator(_utc)]
type CurrencyCode = Annotated[str, AfterValidator(_currency_code)]
type CountryCode = Annotated[str, AfterValidator(_country_code)]
type NonEmptyText = Annotated[str, AfterValidator(_non_empty)]
type Conid = Annotated[int, Field(gt=0)]
