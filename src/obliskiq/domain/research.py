"""Research history: a series with its provenance, its observations and the trials run on it.

A research series is source-tagged: its vendor, the URL it came from, when it was downloaded,
the SHA-256 of what was downloaded, how its availability dates were derived and the biases it is
known to carry. Each observation says what period it describes (``observed_on``) and when it
became public (``available_on``); a read as of a date sees only what was public by then, and a
series with vintages keeps one row per vintage.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import re
from datetime import date
from enum import StrEnum
from typing import Annotated, Self

from pydantic import AfterValidator, Field, model_validator

from .base import CurrencyCode, DomainModel, NonEmptyText, UtcDatetime

_SHA256_RE = re.compile(r"[0-9a-f]{64}")


def _sha256(value: str) -> str:
    if not _SHA256_RE.fullmatch(value):
        raise ValueError("must be a SHA-256 digest in lower-case hexadecimal")
    return value


type Sha256 = Annotated[str, AfterValidator(_sha256)]


class Vendor(StrEnum):
    """Where a research series comes from."""

    YAHOO = "yahoo"
    KEN_FRENCH = "ken_french"
    AQR = "aqr"
    FRED = "fred"
    ALFRED = "alfred"
    ECB = "ecb"
    MSCI = "msci"
    ECB_DATA = "ecb_data"
    BUNDESBANK = "bundesbank"
    #: Computed by the harness from series already in the research store.
    DERIVED = "derived"


class Frequency(StrEnum):
    DAILY = "daily"
    MONTHLY = "monthly"
    ANNUAL = "annual"


class PointInTime(StrEnum):
    """How a series' ``available_on`` dates were derived.

    ``vintages``: the vendor dates each value it published (ALFRED). ``as_observed``: a value is
    public at the close of the day it describes (an exchange close, a reference rate).
    ``release_lag``: no publication date exists; ``available_on`` is the observation date plus
    a declared lag, and the vendor may have revised the value since (labelled in the bias notes).
    """

    VINTAGES = "vintages"
    AS_OBSERVED = "as_observed"
    RELEASE_LAG = "release_lag"


class ResearchSeries(DomainModel):
    """One series of research history and where it came from."""

    series_id: NonEmptyText
    dataset_id: NonEmptyText
    vendor: Vendor
    field: NonEmptyText
    units: NonEmptyText
    currency: CurrencyCode | None
    frequency: Frequency
    point_in_time: PointInTime
    source_url: NonEmptyText
    downloaded_at: UtcDatetime
    content_sha256: Sha256
    bias_notes: tuple[NonEmptyText, ...] = Field(min_length=1)


class Observation(DomainModel):
    """One value of a series: the period it describes and when it became public."""

    observed_on: date
    available_on: date
    value: float

    @model_validator(mode="after")
    def _public_after_it_happened(self) -> Self:
        if self.available_on < self.observed_on:
            raise ValueError("an observation cannot be public before the date it describes")
        return self


class TrialStage(StrEnum):
    EXPLORATION = "exploration"
    CONFIRMATION = "confirmation"


class Trial(DomainModel):
    """One run of a study, exploratory or the confirmation, as the trial registry keeps it.

    ``inputs_sha256`` hashes everything the run read (the specification, every series' content
    hash and the parameters); ``sharpe`` and ``observations`` feed the deflated Sharpe across
    trials.
    """

    trial_id: NonEmptyText
    study_id: NonEmptyText
    stage: TrialStage
    recorded_at: UtcDatetime
    spec_sha256: Sha256
    inputs_sha256: Sha256
    parameters: NonEmptyText
    metric: NonEmptyText
    estimate: float | None
    sharpe: float | None
    observations: Annotated[int, Field(ge=0)] | None
    outcome: NonEmptyText | None
