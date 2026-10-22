"""The market-data collectors' configuration (``collectors``).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from typing import Literal, Self

from pydantic import (
    Field,
    NonNegativeFloat,
    NonNegativeInt,
    PositiveFloat,
    PositiveInt,
    model_validator,
)

from ..domain.base import CountryCode, NonEmptyText
from ..domain.calendar import Mic
from .base import FrozenModel, Identifier
from .schema_calendars import IbkrExchangeCode

#: The rungs ``collectors.chain.sigma_ladder`` may name.
type SigmaRung = Literal["realised_primary", "realised_fallback", "vendor_iv", "default"]
#: The steps of one collection job, in the order they run.
type CollectStep = Literal["universe", "backfill", "snapshots", "chains", "curves"]
COLLECT_STEPS: tuple[CollectStep, ...] = ("universe", "backfill", "snapshots", "chains", "curves")


class RetryConfig(FrozenModel):
    target_retries: NonNegativeInt
    backoff_seconds: PositiveFloat
    backoff_max_seconds: PositiveFloat
    request_spacing_seconds: NonNegativeFloat

    @model_validator(mode="after")
    def _capped_above_its_base(self) -> Self:
        if self.backoff_max_seconds < self.backoff_seconds:
            raise ValueError("backoff_max_seconds must not be below backoff_seconds")
        return self


class CollectScheduleConfig(FrozenModel):
    enabled: bool
    after_close_minutes: NonNegativeInt
    fallback_venue: Mic


class BackfillConfig(FrozenModel):
    window_years: PositiveInt
    overlap_days: PositiveInt


class VolConfig(FrozenModel):
    trading_days_per_year: PositiveFloat
    primary_tenor_days: PositiveInt
    fallback_tenor_days: PositiveInt
    sigma_default: PositiveFloat


class ChainCollectorConfig(FrozenModel):
    sigma_ladder: list[SigmaRung] = Field(min_length=1)
    strike_bounds_sigma: PositiveFloat
    max_expiries_per_underlying: PositiveInt
    max_strikes_per_expiry: PositiveInt
    max_strikes_per_expiry_on_fallback: PositiveInt
    new_listing_grace_days: NonNegativeInt
    bars_freshness_days: PositiveInt

    @model_validator(mode="after")
    def _ladder_terminates(self) -> Self:
        if self.sigma_ladder[-1] != "default":
            raise ValueError("sigma_ladder must end with 'default', so a band always has a sigma")
        if len(set(self.sigma_ladder)) != len(self.sigma_ladder):
            raise ValueError("sigma_ladder names each rung once")
        if self.max_strikes_per_expiry_on_fallback > self.max_strikes_per_expiry:
            raise ValueError("a fallback sigma must not ask for more strikes than the primary one")
        return self


class CurveCollectorConfig(FrozenModel):
    max_expiries_per_root: PositiveInt
    min_expiries_for_roll: PositiveInt


class CandidatesConfig(FrozenModel):
    refresh_days: PositiveInt
    equity_asset_class: NonEmptyText
    date_formats: list[NonEmptyText] = Field(min_length=1)
    name_noise: list[NonEmptyText]
    sectors: dict[NonEmptyText, Identifier]
    locations: dict[NonEmptyText, CountryCode]
    exchanges: dict[NonEmptyText, IbkrExchangeCode]


class CollectorsConfig(FrozenModel):
    retry: RetryConfig
    schedule: CollectScheduleConfig
    step_deadline_seconds: dict[CollectStep, PositiveInt]
    backfill: BackfillConfig
    vol: VolConfig
    chain: ChainCollectorConfig
    curve: CurveCollectorConfig
    candidates: CandidatesConfig

    @model_validator(mode="after")
    def _every_step_has_a_deadline(self) -> Self:
        missing = set(COLLECT_STEPS) - set(self.step_deadline_seconds)
        if missing:
            raise ValueError(f"step_deadline_seconds has no budget for {sorted(missing)}")
        return self
