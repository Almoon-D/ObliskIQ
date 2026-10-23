"""A measurement: one leaf of one entity as of one session, in the long-format store every view
reads (MASTER_PLAN §5.7).

A leaf is measured (``ok``), measured on inputs older than their freshness window (``stale``),
collectible and not collected or refused on too little (``n/a``), or something no collector could
ever fill, as a declaration says (``inapplicable``). Every leaf that is not ``ok`` says why, with a
groupable ``reason_code``; an absent leaf carries no value and a measured one a finite value.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import math
from datetime import date
from enum import StrEnum
from typing import Annotated, Self

from pydantic import NonNegativeInt, StringConstraints, model_validator

from .base import DomainModel, NonEmptyText, UtcDatetime
from .enums import MarketDataStatus

#: The SHA-256 of an input fingerprint, as lower-case hex.
type InputsHash = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class EntityKind(StrEnum):
    """What a measurement is about."""

    EQUITY_CELL = "equity_cell"
    MACRO_CELL = "macro_cell"
    INSTRUMENT = "instrument"
    CORRELATION_PAIR = "correlation_pair"


class MeasurementStatus(StrEnum):
    OK = "ok"
    NA = "n/a"
    STALE = "stale"
    INAPPLICABLE = "inapplicable"

    @property
    def has_value(self) -> bool:
        return self in (MeasurementStatus.OK, MeasurementStatus.STALE)


class Measurement(DomainModel):
    """One stored leaf."""

    entity_kind: EntityKind
    entity_id: NonEmptyText
    indicator: NonEmptyText
    leaf: NonEmptyText
    as_of: date
    value: float | None
    status: MeasurementStatus
    reason: NonEmptyText | None
    reason_code: NonEmptyText | None
    n_obs: NonNegativeInt | None
    window: str | None
    units: str | None
    source: NonEmptyText | None
    market_data_status: MarketDataStatus | None
    inputs_hash: InputsHash
    computed_at: UtcDatetime

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if self.status.has_value != (self.value is not None):
            raise ValueError(f"{self.leaf}: a {self.status.value} leaf has a value exactly when ok")
        if self.value is not None and not math.isfinite(self.value):
            raise ValueError(f"{self.leaf}: a measured value is finite")
        if self.status is not MeasurementStatus.OK and (
            self.reason is None or self.reason_code is None
        ):
            raise ValueError(f"{self.leaf}: a {self.status.value} leaf says why, with a code")
        return self
