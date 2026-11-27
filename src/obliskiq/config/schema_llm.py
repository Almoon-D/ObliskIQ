"""The LLM layer's configuration (``config/llm.yaml``, section ``llm``; MASTER_PLAN §5.10).

A registry of providers and models serves the minimal EventExtractor (DECISIONS D70), the weekly
forecast panel and the Analyst (D156), which runs as several segments side by side, each a scan
model, a dossier model, a prompt version and a share of the one monthly budget (D188). A provider
names the protocol it speaks and the environment variable that holds its key, so a provider is
added by an entry here and one variable; a model carries its prices and the day they were read.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from datetime import date
from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import (
    Field,
    NonNegativeFloat,
    NonNegativeInt,
    PositiveFloat,
    PositiveInt,
    model_validator,
)

from ..domain.base import NonEmptyText
from .base import FrozenModel, Identifier
from .schema_sources import EnvName, Url

#: The transports the layer speaks: the OpenAI-compatible chat completions, and Anthropic's
#: Messages API (``llm/transport.py``).
type LlmProtocol = Literal["openai_compatible", "anthropic"]
#: How hard a model that takes an effort setting thinks (Anthropic's ``output_config.effort``).
type Effort = Literal["low", "medium", "high", "xhigh", "max"]


class LlmProviderConfig(FrozenModel):
    protocol: LlmProtocol
    base_url: Url
    key_env: EnvName
    #: The API version a provider requires in its requests (Anthropic's ``anthropic-version``).
    api_version: NonEmptyText | None = None

    @model_validator(mode="after")
    def _versioned(self) -> Self:
        if (self.protocol == "anthropic") != (self.api_version is not None):
            raise ValueError("an anthropic provider names its api_version, and only it does")
        return self


class LlmModelConfig(FrozenModel):
    provider: Identifier
    model_id: NonEmptyText
    context_window_tokens: PositiveInt
    input_usd_per_million: NonNegativeFloat
    output_usd_per_million: NonNegativeFloat
    #: The day the prices were read in the provider's own documentation.
    prices_read: date
    #: The effort the model is asked to think at, where its protocol takes one.
    effort: Effort | None = None
    #: The training cutoff its provider documents (``YYYY-MM``), where it documents one; a model
    #: without one is ``no_cutoff`` and is never replayed (H15, D191).
    training_cutoff: Annotated[str, Field(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")] | None = None
    #: Whether that cutoff is stated for this model or inherited from the model its card names.
    cutoff_source: Literal["stated", "inherited"] | None = None

    @model_validator(mode="after")
    def _cutoff_has_source(self) -> Self:
        if (self.training_cutoff is None) != (self.cutoff_source is None):
            raise ValueError("a training cutoff and its source are given together")
        return self


class ReplayConfig(FrozenModel):
    """The analyst's replay after its models' cutoff (H15, DECISIONS D191)."""

    #: The most the replay may spend in all, in the providers' price currency: the owner's.
    max_usd: NonNegativeFloat
    #: The replay's own store, in the data directory: its record never meets the forward one.
    store_file: NonEmptyText
    #: The SEC's quarterly index of XBRL filings, and its bulk file of every company's facts.
    full_index_url_template: Annotated[
        str, Field(pattern=r"^https://\S*\{year\}\S*\{quarter\}\S*$")
    ]
    companyfacts_zip_url: Annotated[str, Field(pattern=r"^https://\S+\.zip$")]
    companyfacts_member_template: Annotated[str, Field(pattern=r"^\S*\{cik(:[0-9a-z]+)?\}\S*$")]
    #: The venue whose sessions count a call's horizon.
    session_venue: Annotated[str, Field(pattern=r"^[A-Z0-9]{4}$")]
    #: The research dataset of reference rates a round trip's cost is converted with.
    fx_dataset: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$")]
    #: The forms that make a company a filer, and how far back a filing counts.
    filer_forms: list[NonEmptyText] = Field(min_length=1)
    filer_lookback_months: PositiveInt
    #: Days of closes loaded before the first sheet: the screen's trend window and its slack.
    history_days: PositiveInt
    #: A probe's answer is one probability.
    probe_max_output_tokens: PositiveInt


class LlmBudgetConfig(FrozenModel):
    monthly_usd: NonNegativeFloat
    chars_per_token_estimate: PositiveFloat


class ExtractorConfig(FrozenModel):
    model: Identifier
    prompt_version: PositiveInt
    temperature: NonNegativeFloat
    max_output_tokens: PositiveInt
    max_items_per_run: PositiveInt
    max_item_chars: PositiveInt
    request_timeout_seconds: PositiveInt
    retries_on_invalid: NonNegativeInt
    max_call_failures: PositiveInt


class FactSheetConfig(FrozenModel):
    """What the analyst's fact sheet carries (``grid/facts.py``)."""

    grid_leaves: list[NonEmptyText] = Field(min_length=1)
    return_sessions: list[PositiveInt] = Field(min_length=1)
    price_stale_after_days: PositiveInt
    news_lookback_days: PositiveInt
    max_news_lines: NonNegativeInt
    max_market_lines: NonNegativeInt
    max_insider_lines: NonNegativeInt
    stock_screen_top: NonNegativeInt


class AnalystSegmentConfig(FrozenModel):
    """One analyst segment: the model that scans the sheet, the model that writes the dossiers
    and the reviews, the prompt's version and the share of the monthly budget it may spend."""

    scan_model: Identifier
    dossier_model: Identifier
    prompt_version: PositiveInt
    budget_share: Annotated[Decimal, Field(gt=0, le=1)]


class ScanConfig(FrozenModel):
    """The daily scan: the candidates one call may name."""

    max_candidates: PositiveInt
    max_output_tokens: PositiveInt


class DossierConfig(FrozenModel):
    """The dossier per candidate: the sections every dossier reads beside the candidate's lines."""

    max_output_tokens: PositiveInt
    context_sections: list[NonEmptyText]


class ReviewConfig(FrozenModel):
    """The weekly review of each open thesis and open call."""

    max_output_tokens: PositiveInt
    max_theses_per_week: NonNegativeInt


class AnalystConfig(FrozenModel):
    """The Analyst role: theses of its own and second opinions, by segment (``llm/analyst.py``)."""

    segments: dict[Identifier, AnalystSegmentConfig] = Field(min_length=1)
    #: The segment whose drafts may take the book's slots; the others run in shadow and are
    #: graded in the idea ledger. An owner setting (DECISIONS D177).
    trading_segment: Identifier
    scan: ScanConfig
    dossier: DossierConfig
    review: ReviewConfig
    temperature: NonNegativeFloat
    request_timeout_seconds: PositiveInt
    retries_on_invalid: NonNegativeInt
    max_call_failures: PositiveInt
    max_drafts_per_week: NonNegativeInt
    max_drafts_per_run: NonNegativeInt
    max_horizon_days: PositiveInt
    fact_sheet: FactSheetConfig


class PanelConfig(FrozenModel):
    """The weekly forecast panel (``llm/panel.py``): the analyst's model and budget; the
    pre-registration's horizon, mark age and minimums."""

    model: Identifier
    prompt_version: PositiveInt
    temperature: NonNegativeFloat
    max_output_tokens: PositiveInt
    request_timeout_seconds: PositiveInt
    retries_on_invalid: NonNegativeInt
    batch_size: PositiveInt
    min_batch_coverage: Annotated[Decimal, Field(gt=0, le=1)]
    max_consecutive_failures: PositiveInt
    batch_pause_seconds: NonNegativeFloat
    min_history_sessions: PositiveInt
    return_sessions: list[PositiveInt] = Field(min_length=1)
    momentum_lookback_sessions: PositiveInt
    momentum_skip_sessions: PositiveInt
    volatility_sessions: PositiveInt
    min_volatility_returns: PositiveInt
    high_sessions: PositiveInt
    annualisation_sessions: PositiveInt
    probability_bounds: tuple[
        Annotated[Decimal, Field(gt=0, lt=1)], Annotated[Decimal, Field(gt=0, lt=1)]
    ]
    max_abs_excess_pct: Annotated[Decimal, Field(gt=0)]
    asked_excess_range_pct: Annotated[Decimal, Field(gt=0)]

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        low, high = self.probability_bounds
        if not low < high:
            raise ValueError("probability_bounds runs from the lower bound to the upper")
        if self.momentum_skip_sessions >= self.momentum_lookback_sessions:
            raise ValueError("12-1 momentum skips fewer sessions than it looks back")
        if self.asked_excess_range_pct > self.max_abs_excess_pct:
            raise ValueError("the range asked for lies inside the limit an answer is held to")
        needed = max(*self.return_sessions, self.volatility_sessions) + 1
        if self.min_history_sessions < needed:
            raise ValueError(f"min_history_sessions is below the {needed} the statistics need")
        return self


class LlmConfig(FrozenModel):
    providers: dict[Identifier, LlmProviderConfig] = Field(min_length=1)
    models: dict[Identifier, LlmModelConfig] = Field(min_length=1)
    budget: LlmBudgetConfig
    extractor: ExtractorConfig
    analyst: AnalystConfig
    panel: PanelConfig
    replay: ReplayConfig

    def provider_of(self, model: str) -> LlmProviderConfig:
        return self.providers[self.models[model].provider]

    def segment_order(self) -> list[str]:
        """The analyst's segments, the trading segment first."""
        trading = self.analyst.trading_segment
        return [trading, *(name for name in self.analyst.segments if name != trading)]

    def panel_model(self) -> LlmModelConfig:
        return self.models[self.panel.model]

    def panel_provider(self) -> LlmProviderConfig:
        return self.provider_of(self.panel.model)

    def extractor_model(self) -> LlmModelConfig:
        return self.models[self.extractor.model]

    def extractor_provider(self) -> LlmProviderConfig:
        return self.providers[self.extractor_model().provider]

    @model_validator(mode="after")
    def _references_resolve(self) -> Self:
        problems = [
            f"models.{name} names provider {model.provider}, which is not declared"
            for name, model in self.models.items()
            if model.provider not in self.providers
        ]
        roles = [("extractor.model", self.extractor.model), ("panel.model", self.panel.model)]
        for name, segment in self.analyst.segments.items():
            roles += [
                (f"analyst.segments.{name}.scan_model", segment.scan_model),
                (f"analyst.segments.{name}.dossier_model", segment.dossier_model),
            ]
        problems += [
            f"{role} {model} is not a declared model"
            for role, model in roles
            if model not in self.models
        ]
        if self.analyst.trading_segment not in self.analyst.segments:
            problems.append(
                f"analyst.trading_segment {self.analyst.trading_segment} is not a declared segment"
            )
        shares = sum(segment.budget_share for segment in self.analyst.segments.values())
        if shares > 1:
            problems.append(f"the analyst segments' budget shares add up to {shares}, above 1")
        if problems:
            raise ValueError("; ".join(problems))
        return self
