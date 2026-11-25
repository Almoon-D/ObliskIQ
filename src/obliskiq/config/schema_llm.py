"""The LLM layer's configuration (``config/llm.yaml``, section ``llm``; MASTER_PLAN §5.10).

One provider and one model serve two roles: the minimal EventExtractor (DECISIONS D70) and the
Analyst with its fact sheet (D156); the registry grows in Session 16. A provider names the
environment variable that holds its key, so a provider is added by an entry here and one variable.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

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

#: The transports the layer speaks (Session 16 adds the native adapters).
type LlmProtocol = Literal["openai_compatible"]


class LlmProviderConfig(FrozenModel):
    protocol: LlmProtocol
    base_url: Url
    key_env: EnvName


class LlmModelConfig(FrozenModel):
    provider: Identifier
    model_id: NonEmptyText
    context_window_tokens: PositiveInt
    input_usd_per_million: NonNegativeFloat
    output_usd_per_million: NonNegativeFloat


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


class AnalystConfig(FrozenModel):
    """The Analyst role: theses of its own and second opinions (``llm/analyst.py``)."""

    model: Identifier
    prompt_version: PositiveInt
    temperature: NonNegativeFloat
    max_output_tokens: PositiveInt
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

    def analyst_model(self) -> LlmModelConfig:
        return self.models[self.analyst.model]

    def analyst_provider(self) -> LlmProviderConfig:
        return self.providers[self.analyst_model().provider]

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
        for role, name in (("extractor", self.extractor.model), ("analyst", self.analyst.model)):
            if name not in self.models:
                problems.append(f"{role}.model {name} is not a declared model")
        if problems:
            raise ValueError("; ".join(problems))
        return self
