"""A pre-registration file: its machine-readable front matter and its prose (DECISIONS D63).

A pre-registration is a Markdown file from ``research/preregistrations/TEMPLATE.md``. Its front
matter, between two ``---`` lines at the top, holds every field D63 names — the mechanism, the
universe, the data and their point-in-time status, the primary metric, the horizon, theta, the
power inputs, the adjustment, the robustness checks and the trial budget — in a form the harness
runs; the prose below explains them. Nothing has a default the harness picks silently: an
optional field left out takes the value the configuration names (D62's alpha and target power),
and the report says which.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Literal, Self

import yaml
from pydantic import Field, PositiveInt, ValidationError, model_validator

from ..config.base import FrozenModel
from ..config.loader import strict_yaml
from ..config.schema_costs import Rate
from ..domain.base import CountryCode, CurrencyCode, NonEmptyText
from ..domain.enums import AssetClass
from ..domain.research import PointInTime
from ..domain.tax import Isin, SecurityKind
from ..errors import PreregistrationError
from .registry import file_sha256

#: The line that opens and closes the front matter.
FRONT_MATTER_FENCE = "---"

type Positive = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
type Key = Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]+$")]


class SecuritySpec(FrozenModel):
    kind: SecurityKind
    isin: Isin | None
    issuer: NonEmptyText | None
    tracked_index: NonEmptyText | None
    listed: bool


class InstrumentSpec(FrozenModel):
    """One instrument of the study and where its history is."""

    key: Key
    price_series: NonEmptyText
    income_series: NonEmptyText | None
    currency: CurrencyCode
    venue: NonEmptyText
    asset_class: AssetClass
    issuer_country: CountryCode | None
    income_country: CountryCode | None
    market_cap_usd: Positive | None
    security: SecuritySpec


class StrategySpec(FrozenModel):
    name: NonEmptyText
    strategy: NonEmptyText
    weights: dict[Key, Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]]


class StudySpec(FrozenModel):
    """What is simulated: the instruments, the dates, the capital and the two strategies."""

    start: date
    end: date
    rebalance: Literal["month_end"]
    calendar: Key
    initial_capital: Positive
    fx_dataset: NonEmptyText | None
    instruments: list[InstrumentSpec] = Field(min_length=1)
    candidate: StrategySpec
    reference: StrategySpec

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if self.end <= self.start:
            raise ValueError("end must follow start")
        keys = [instrument.key for instrument in self.instruments]
        if len(keys) != len(set(keys)):
            raise ValueError("instrument keys must be unique")
        if self.calendar not in keys:
            raise ValueError(f"calendar {self.calendar} is not one of the instruments")
        for strategy in (self.candidate, self.reference):
            unknown = sorted(set(strategy.weights) - set(keys))
            if unknown:
                raise ValueError(f"strategy {strategy.name} weights unknown instruments {unknown}")
        return self


class BootstrapSpec(FrozenModel):
    n_boot: PositiveInt
    block_months: PositiveInt
    seed: Annotated[int, Field(ge=0)]


class AdequacySpec(FrozenModel):
    """The checks the data must pass before anything is inferred (D62)."""

    min_coverage: Annotated[Decimal, Field(gt=0, le=1)]
    point_in_time: list[PointInTime] = Field(min_length=1)
    min_effective_observations: PositiveInt


class PowerSpec(FrozenModel):
    """What the power calculation assumes before the data are read: the standard deviation of
    one observation (one name in one period) of the primary metric, the periods, how many
    periods make a year, the names per period and their average correlation."""

    sd_per_period: Positive
    periods: PositiveInt
    periods_per_year: PositiveInt
    names_per_period: PositiveInt
    rho: Annotated[Decimal, Field(ge=-1, le=1)]


class PreregSpec(FrozenModel):
    """Every field of a pre-registration (D63)."""

    id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]*$")]
    title: NonEmptyText
    kind: Literal["portfolio_comparison"]
    mechanism: NonEmptyText
    citation: NonEmptyText
    universe: NonEmptyText
    data_status: NonEmptyText
    primary_metric: Literal["log_growth_difference"]
    theta: Positive
    theta_justification: NonEmptyText
    alpha: Rate | None
    target_power: Rate | None
    tests_in_confirmation: PositiveInt
    adjustment: NonEmptyText
    trial_budget: PositiveInt
    study: StudySpec
    bootstrap: BootstrapSpec
    drawdown_limits: list[Annotated[Decimal, Field(gt=0, lt=1)]] = Field(min_length=1)
    subperiods: list[tuple[date, date]]
    cost_uncertainty_multiplier: Positive
    robustness: NonEmptyText
    adequacy: AdequacySpec
    power: PowerSpec

    @model_validator(mode="after")
    def _subperiods_inside_the_study(self) -> Self:
        for first, last in self.subperiods:
            if not self.study.start <= first < last <= self.study.end:
                raise ValueError(f"subperiod {first} to {last} is not inside the study")
        return self


@dataclass(frozen=True, slots=True)
class Prereg:
    path: Path
    sha256: str
    spec: PreregSpec
    body: str


def front_matter(text: str) -> tuple[Any, str]:
    """The YAML between the opening fences, and the prose after them."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != FRONT_MATTER_FENCE:
        raise PreregistrationError("a pre-registration opens with a front matter fence (---)")
    try:
        close = next(i for i, line in enumerate(lines[1:], 1) if line.strip() == FRONT_MATTER_FENCE)
    except StopIteration:
        raise PreregistrationError("the front matter has no closing fence (---)") from None
    try:
        data = strict_yaml("\n".join(lines[1:close]))
    except yaml.YAMLError as exc:
        raise PreregistrationError(f"the front matter is not valid YAML: {exc}") from None
    return data, "\n".join(lines[close + 1 :])


def load_prereg(path: Path) -> Prereg:
    """Read and validate a pre-registration; every problem is named."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PreregistrationError(f"{path} cannot be read: {exc}") from None
    data, body = front_matter(text)
    try:
        spec = PreregSpec.model_validate(data)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in error['loc'])}: {error['msg']}" for error in exc.errors()
        )
        raise PreregistrationError(
            f"{path.name} is not a valid pre-registration: {problems}"
        ) from None
    return Prereg(path=path, sha256=file_sha256(path), spec=spec, body=body)
