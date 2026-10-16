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
    frequency: Literal["monthly", "annual"]
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


type Dataset = Annotated[
    YahooDataset | KenFrenchDataset | AqrDataset | FredDataset | EcbDataset,
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


class ResearchConfig(FrozenModel):
    reports_dir: NonEmptyText
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
