"""Macro observations, aligned on the date they became public.

A CPI print aligned to the month it describes lets every number derived from it know the
figure weeks before the market did. An observation therefore carries both dates, and a series
read for a date holds only what had been released by then.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from datetime import date
from typing import Self

from pydantic import NonNegativeInt, model_validator

from .base import DomainModel, NonEmptyText, UtcDatetime


class MacroObservation(DomainModel):
    """One value of one series for one reference period.

    ``release_date`` is when the value became public and is what readers align on; ``None``
    means the source cannot say, which makes the value unusable point-in-time. A re-pull of the
    same period overwrites the value and its release date (one vintage per period).
    """

    series_id: NonEmptyText
    source: NonEmptyText
    reference_date: date
    release_date: date | None
    value: float
    frequency: NonEmptyText | None
    units: NonEmptyText | None
    collected_at: UtcDatetime


class MacroSeries(DomainModel):
    """The observations of one series that were public on ``as_of``, oldest reference first.

    ``unaligned`` counts the observations the store holds that have no release date: they are
    left out, and a caller reports the coverage gap instead of aligning them optimistically.
    """

    series_id: NonEmptyText
    source: NonEmptyText | None
    as_of: date
    observations: tuple[MacroObservation, ...]
    unaligned: NonNegativeInt

    @model_validator(mode="after")
    def _only_what_was_public(self) -> Self:
        references = [observation.reference_date for observation in self.observations]
        if references != sorted(references):
            raise ValueError("observations must be ordered by reference date")
        for observation in self.observations:
            if observation.release_date is None or observation.release_date > self.as_of:
                raise ValueError("a series holds only observations released on or before as_of")
        return self

    def __len__(self) -> int:
        return len(self.observations)

    @property
    def latest(self) -> MacroObservation | None:
        """The newest published observation by reference date."""
        return self.observations[-1] if self.observations else None

    def values(self) -> list[float]:
        return [observation.value for observation in self.observations]

    @property
    def units(self) -> frozenset[str]:
        """The distinct declared units. Two units in one window are two different quantities.

        An undeclared unit (``None``) is not counted: a row that never said what it carries
        cannot contradict one that did.
        """
        return frozenset(o.units for o in self.observations if o.units is not None)

    @property
    def frequencies(self) -> frozenset[str]:
        """The distinct declared frequencies; undeclared ones are not counted."""
        return frozenset(o.frequency for o in self.observations if o.frequency is not None)
