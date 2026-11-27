"""The analyst replayed after its models' training cutoff (``kind: analyst_replay``, H15;
DECISIONS D191): its pre-registration's front matter.

A replay rebuilds the analyst's fact sheet as it stood on dates its models could not know, asks
the registered segments for their calls on it and grades them on prices that already exist. The
front matter fixes the models and their documented cutoffs, the segments, the dates and the
margin, the sheet's sections, the call, the outcome, the statistic and its D62 rule, the leak
measure, the memory probe, the calibration, power, and the only things a verdict may change.
Every field is checked here against the others, so a file that contradicts itself is refused
before the harness reads it. ``research/replay/`` runs it.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from datetime import date
from decimal import Decimal
from typing import Annotated, Final, Literal, Self

from pydantic import Field, PositiveInt, model_validator

from ..config.base import FrozenModel
from ..config.schema_costs import Rate
from ..domain.base import NonEmptyText

type _Name = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$")]
type _Month = Annotated[str, Field(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")]
type _Positive = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
type _Share = Annotated[Decimal, Field(gt=0, lt=1, allow_inf_nan=False)]
type _Correlation = Annotated[Decimal, Field(ge=-1, le=1, allow_inf_nan=False)]

#: The sections a replay sheet may hold: those rebuilt from sources that date their publication.
REPLAYABLE_SECTIONS = ("instrument", "stock_screen")


#: What the replay never touches (the registration names all of them).
NEVER: Final = ("capital", "size", "slot", "gate", "h13", "h14", "pooled_records")


class ReplayModelSpec(FrozenModel):
    """A model replayed: its config name, its provider's id and the cutoff its provider
    documents, read on ``cutoff_read``."""

    name: _Name
    model_id: NonEmptyText
    snapshot: Literal["stable", "pinned"]
    training_cutoff: _Month
    cutoff_source: Literal["stated", "inherited"]
    cutoff_read: date
    cutoff_evidence: NonEmptyText


class NotReplayedSpec(FrozenModel):
    model_id: NonEmptyText
    status: Literal["no_key", "no_cutoff"]
    reason: NonEmptyText


class ReplaySegmentSpec(FrozenModel):
    name: _Name
    scan_model: _Name
    dossier_model: _Name


class WindowSpec(FrozenModel):
    first: date
    last: date
    sheets: PositiveInt

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.last < self.first:
            raise ValueError("a window's last sheet is not before its first")
        return self


class ReplayDatesSpec(FrozenModel):
    cutoff_day: Literal["last_day_of_cutoff_month"]
    margin_days: PositiveInt
    weekday: Literal["monday"]
    after: WindowSpec
    before: WindowSpec
    inside_margin: Literal["refused"]

    @model_validator(mode="after")
    def _apart(self) -> Self:
        if self.before.last >= self.after.first:
            raise ValueError("the leak control ends before the window after the cutoff starts")
        if self.before.sheets != self.after.sheets:
            raise ValueError("the leak control has as many sheets as the window after the cutoff")
        return self


class ReplaySheetSpec(FrozenModel):
    sections: list[NonEmptyText] = Field(min_length=1)
    omitted: list[NonEmptyText] = Field(min_length=1)
    record: Literal["replay"]
    shape_in_segment_hash: Literal[True]

    @model_validator(mode="after")
    def _rebuildable(self) -> Self:
        unknown = sorted(set(self.sections) - set(REPLAYABLE_SECTIONS))
        if unknown:
            raise ValueError(f"no source dates its own publication for {', '.join(unknown)}")
        if set(self.sections) & set(self.omitted):
            raise ValueError("a section is either on the sheet or omitted")
        return self


class ReplayCallSpec(FrozenModel):
    passes: list[Literal["scan", "dossier"]] = Field(min_length=2, max_length=2)
    max_candidates: PositiveInt
    temperature: Annotated[Decimal, Field(ge=0)]
    tools: Literal["none"]
    answer: Literal["forward_schema_validated_one_retry"]


class ReplayOutcomeSpec(FrozenModel):
    horizon_sessions: PositiveInt
    start: Literal["first_close_after_sheet_date"]
    benchmark: Literal["equal_weight_evaluable_universe"]
    secondary: list[Literal["equity_reference", "ledger_levels"]] = Field(min_length=1)
    costs: Literal["cost_model_round_trip"]
    cost_trade_value: _Positive
    max_mark_age_days: PositiveInt
    not_evaluable_cohort_share: _Share


class ReplayStatisticSpec(FrozenModel):
    outcome: Literal["excess_log_return_after_costs"]
    calls: Literal["stock_calls"]
    cohort: Literal["sheet_week"]
    estimate: Literal["mean_of_cohort_means"]
    newey_west_lags: PositiveInt
    rho: _Correlation
    alpha: Rate
    theta: _Positive
    min_cohorts: PositiveInt
    min_effective: _Positive
    adjustment: Literal["bonferroni_over_segments"]


class ReplayLeakSpec(FrozenModel):
    measure: Literal["before_minus_after"]
    remembering: Literal["sufficient_before_only"]


class ReplayProbeSpec(FrozenModel):
    questions_per_quarter: PositiveInt
    seed: int
    asks: Literal["probability_close_higher_after_horizon"]
    given: Literal["ticker_name_and_date_only"]
    scored_against: Literal["majority_outcome_of_probe_sample"]
    test: Literal["one_sided_binomial"]
    alpha: Rate
    recalled_quarter_after_cutoff: Literal["trimmed_from_primary"]
    control: Literal["quarters_before_cutoff"]


class ReplayCalibrationSpec(FrozenModel):
    probability: Literal["target_first"]
    resolution: Literal["ledger_levels_resolved_by_price_end"]
    base_rate: Literal["segment_climatology"]
    score: Literal["brier"]
    reliability_bins: PositiveInt
    min_cohorts: PositiveInt
    min_effective: _Positive


class ReplayPowerSpec(FrozenModel):
    sd_per_call: _Positive
    calls_per_cohort: PositiveInt
    effective_after: _Positive
    detectable_at_half: _Positive
    detects_theta: bool


class AnalystReplaySpec(FrozenModel):
    """The analyst replayed after its models' training cutoff (``kind: analyst_replay``)."""

    id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]*$")]
    title: NonEmptyText
    kind: Literal["analyst_replay"]
    hypothesis: Annotated[str, Field(pattern=r"^H[0-9]+$")]
    mechanism: NonEmptyText
    citation: NonEmptyText
    universe: NonEmptyText
    data_status: NonEmptyText
    models: list[ReplayModelSpec] = Field(min_length=1)
    not_replayed: list[NotReplayedSpec] = Field(default_factory=list)
    segments: list[ReplaySegmentSpec] = Field(min_length=1)
    dates: ReplayDatesSpec
    sheet: ReplaySheetSpec
    call: ReplayCallSpec
    outcome: ReplayOutcomeSpec
    statistic: ReplayStatisticSpec
    leak: ReplayLeakSpec
    probe: ReplayProbeSpec
    calibration: ReplayCalibrationSpec
    decides: list[Literal["forward_budget_shares", "proposed_trading_segment"]] = Field(
        min_length=2, max_length=2
    )
    never: list[Literal["capital", "size", "slot", "gate", "h13", "h14", "pooled_records"]] = Field(
        min_length=len(NEVER)
    )
    budget_key: Literal["llm.replay.max_usd"]
    trial_budget: Literal[0]
    power: ReplayPowerSpec

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        names = {model.name for model in self.models}
        for segment in self.segments:
            missing = {segment.scan_model, segment.dossier_model} - names
            if missing:
                raise ValueError(
                    f"segment {segment.name} uses {', '.join(sorted(missing))}, not a replayed "
                    "model"
                )
        if len({s.name for s in self.segments}) != len(self.segments):
            raise ValueError("a segment is registered once")
        if set(self.never) != set(NEVER):
            raise ValueError("the replay reaches no capital, size, slot, gate, H13, H14 or pool")
        if self.calibration.min_effective < self.statistic.min_effective:
            raise ValueError("calibration needs at least the record's minimum")
        return self

    def model(self, name: str) -> ReplayModelSpec:
        return next(model for model in self.models if model.name == name)

    def segment_cutoff(self, segment: ReplaySegmentSpec) -> str:
        """A segment's cutoff: the later of its models' (``YYYY-MM`` sorts as text)."""
        return max(
            self.model(segment.scan_model).training_cutoff,
            self.model(segment.dossier_model).training_cutoff,
        )
