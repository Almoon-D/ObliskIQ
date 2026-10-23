"""The LLM layer's configuration (``config/llm.yaml``, section ``llm``; MASTER_PLAN §5.10).

Session 10 holds one provider, one model and the minimal EventExtractor (DECISIONS D70); the
registry grows in Session 16. A provider names the environment variable that holds its key, so a
provider is added by an entry here and one variable.

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


class LlmConfig(FrozenModel):
    providers: dict[Identifier, LlmProviderConfig] = Field(min_length=1)
    models: dict[Identifier, LlmModelConfig] = Field(min_length=1)
    budget: LlmBudgetConfig
    extractor: ExtractorConfig

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
        if self.extractor.model not in self.models:
            problems.append(f"extractor.model {self.extractor.model} is not a declared model")
        if problems:
            raise ValueError("; ".join(problems))
        return self
