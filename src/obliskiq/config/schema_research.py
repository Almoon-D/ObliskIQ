"""The shape of the research configuration: the decision defaults, the simulator's mechanics, the
data vendors and the datasets ``obliskiq research data load`` knows.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from datetime import date
from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import Field, NonNegativeInt, PositiveFloat, PositiveInt, model_validator

from ..domain.base import CurrencyCode, NonEmptyText
from .base import FrozenModel
from .schema_costs import Fraction, Rate

type Notes = Annotated[list[NonEmptyText], Field(min_length=1)]
type PositiveDecimal = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
type DatasetId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$")]


class DecisionConfig(FrozenModel):
    alpha: Rate
    target_power: Rate


class SimulationConfig(FrozenModel):
    no_trade_band_fraction: Fraction
    max_price_age_days: NonNegativeInt
    whole_units: bool
    days_per_year: PositiveDecimal


class BreakevenConfig(FrozenModel):
    max_multiplier: PositiveDecimal
    multiplier_tolerance: PositiveDecimal


class KenFrenchVendor(FrozenModel):
    base_url: NonEmptyText
    missing_markers: list[NonEmptyText]
    bias_notes: Notes


class AqrVendor(FrozenModel):
    base_url: NonEmptyText
    bias_notes: Notes


class FredVendor(FrozenModel):
    csv_url: NonEmptyText
    api_url: NonEmptyText
    realtime_start: date
    realtime_end: date
    bias_notes: Notes


class EcbVendor(FrozenModel):
    url: NonEmptyText
    bias_notes: Notes


class YahooVendor(FrozenModel):
    bias_notes: Notes
    universe_start: date
    suffixes: dict[NonEmptyText, Annotated[str, Field(pattern=r"^(\.[A-Z]{1,3})?$")]]
    digits: dict[NonEmptyText, PositiveInt]


class MsciVendor(FrozenModel):
    url: NonEmptyText
    # The endpoint refuses a start before this date.
    earliest_start: date
    bias_notes: Notes


class EcbDataVendor(FrozenModel):
    base_url: NonEmptyText
    bias_notes: Notes


class BundesbankVendor(FrozenModel):
    base_url: NonEmptyText
    bias_notes: Notes


class DerivedVendor(FrozenModel):
    bias_notes: Notes


class YahooDataset(FrozenModel):
    id: DatasetId
    vendor: Literal["yahoo"]
    symbol: NonEmptyText
    currency: CurrencyCode
    start: date
    bias_notes: Notes


class KenFrenchDataset(FrozenModel):
    id: DatasetId
    vendor: Literal["ken_french"]
    file: NonEmptyText
    section: NonNegativeInt
    frequency: Literal["daily", "monthly", "annual"]
    columns: list[NonEmptyText] | None
    release_lag_days: NonNegativeInt
    bias_notes: Notes


class AqrDataset(FrozenModel):
    id: DatasetId
    vendor: Literal["aqr"]
    file: NonEmptyText
    sheet: NonEmptyText
    header_row: PositiveInt
    columns: list[NonEmptyText] = Field(min_length=1)
    units: Literal["decimal", "percent"]
    # How column A spells a date: an Excel serial number, or text as month/day/year.
    date_format: Literal["excel_serial", "month_day_year"]
    release_lag_days: NonNegativeInt
    bias_notes: Notes


class FredDataset(FrozenModel):
    id: DatasetId
    vendor: Literal["fred"]
    series: NonEmptyText
    vintages: bool
    frequency: Literal["daily", "monthly", "annual"]
    units: NonEmptyText
    release_lag_days: NonNegativeInt
    start: date
    bias_notes: Notes


class EcbDataset(FrozenModel):
    id: DatasetId
    vendor: Literal["ecb"]
    currencies: list[CurrencyCode] = Field(min_length=1)
    bias_notes: Notes


class MsciDataset(FrozenModel):
    """Daily index levels from MSCI's public end-of-day endpoint."""

    id: DatasetId
    vendor: Literal["msci"]
    index_code: Annotated[str, Field(pattern=r"^[0-9]+$")]
    variant: Literal["STRD", "NETR", "GRTR"]
    currency: CurrencyCode
    start: date
    bias_notes: Notes


class EcbDataDataset(FrozenModel):
    """One daily series of the ECB Data Portal (SDMX), by dataflow and key."""

    id: DatasetId
    vendor: Literal["ecb_data"]
    flow: NonEmptyText
    key: NonEmptyText
    units: NonEmptyText
    start: date
    bias_notes: Notes


class BundesbankDataset(FrozenModel):
    """One daily series of the Bundesbank's time-series database, by dataflow and key."""

    id: DatasetId
    vendor: Literal["bundesbank"]
    flow: NonEmptyText
    key: NonEmptyText
    units: NonEmptyText
    start: date
    bias_notes: Notes


class AccrualSegment(FrozenModel):
    """A rate series used for the days up to ``through`` (``None``: to its last day)."""

    series: NonEmptyText
    through: date | None


class OvernightAccrualDataset(FrozenModel):
    """An index that accrues overnight rates (percent a year) day by day: the rate fixed for a
    day applies until the next published day, on an actual/``day_count_basis`` count."""

    id: DatasetId
    vendor: Literal["derived"]
    method: Literal["overnight_accrual"]
    segments: list[AccrualSegment] = Field(min_length=1)
    day_count_basis: PositiveInt
    currency: CurrencyCode
    base_level: PositiveDecimal
    bias_notes: Notes

    @model_validator(mode="after")
    def _segments_in_order(self) -> Self:
        ends = [segment.through for segment in self.segments]
        if any(end is None for end in ends[:-1]) or ends[-1] is not None:
            raise ValueError("only the last segment runs open-ended, and it must")
        bounded = [end for end in ends if end is not None]
        if bounded != sorted(set(bounded)):
            raise ValueError("segments must end in strictly rising order")
        return self


class ConstantMaturityZeroDataset(FrozenModel):
    """The index of a zero-coupon bond kept at one maturity: bought at ``maturity_years`` each
    day and sold the next at the curve's yield for the maturity left, interpolated linearly
    between ``maturity_years`` and ``shorter_maturity_years``."""

    id: DatasetId
    vendor: Literal["derived"]
    method: Literal["constant_maturity_zero"]
    yield_series: NonEmptyText
    maturity_years: PositiveInt
    shorter_yield_series: NonEmptyText
    shorter_maturity_years: PositiveInt
    compounding: Literal["continuous", "annual"]
    year_basis_days: PositiveDecimal
    currency: CurrencyCode
    base_level: PositiveDecimal
    bias_notes: Notes

    @model_validator(mode="after")
    def _shorter_is_shorter(self) -> Self:
        if self.shorter_maturity_years >= self.maturity_years:
            raise ValueError("the shorter maturity must be below the maturity")
        return self


type DerivedDataset = Annotated[
    OvernightAccrualDataset | ConstantMaturityZeroDataset, Field(discriminator="method")
]

type Dataset = Annotated[
    YahooDataset
    | KenFrenchDataset
    | AqrDataset
    | FredDataset
    | EcbDataset
    | MsciDataset
    | EcbDataDataset
    | BundesbankDataset
    | DerivedDataset,
    Field(discriminator="vendor"),
]


class ResearchDataConfig(FrozenModel):
    timeout_seconds: PositiveFloat
    max_fx_age_days: NonNegativeInt
    ken_french: KenFrenchVendor
    aqr: AqrVendor
    fred: FredVendor
    ecb: EcbVendor
    yahoo: YahooVendor
    msci: MsciVendor
    ecb_data: EcbDataVendor
    bundesbank: BundesbankVendor
    derived: DerivedVendor


class ResearchConfig(FrozenModel):
    reports_dir: NonEmptyText
    workers: PositiveInt
    decision: DecisionConfig
    simulation: SimulationConfig
    breakeven: BreakevenConfig
    data: ResearchDataConfig
    datasets: list[Dataset]

    @model_validator(mode="after")
    def _dataset_ids_are_unique(self) -> Self:
        ids = [dataset.id for dataset in self.datasets]
        if len(ids) != len(set(ids)):
            raise ValueError("datasets ids must be unique")
        return self

    def dataset(self, dataset_id: str) -> Dataset | None:
        return next((d for d in self.datasets if d.id == dataset_id), None)
