"""
Shared test doubles for the AI layer.

``ScriptedProvider`` is a real ``LLMProvider`` whose responses are scripted, so tests
exercise the analyst's actual prompt -> clean -> contract-validate -> retry path and
the router's actual failover logic without any network or SDK.

Each scripted item is either response TEXT (returned) or an Exception (raised), in
call order; running out of script is a test bug and raises loudly.
"""

from __future__ import annotations

import dataclasses
from typing import List, Optional, Tuple, Union

from modules.analysis.providers import LLMProvider
from modules.config.ai_providers import PROVIDER_SPECS, ProviderSpec

Scripted = Union[str, Exception]


class ScriptedProvider(LLMProvider):
    """A provider that replays a script and records every prompt it received."""

    def __init__(
        self,
        responses: List[Scripted],
        *,
        name: str = "gemini",
        spec: Optional[ProviderSpec] = None,
        model: Optional[str] = None,
        max_input_tokens: Optional[int] = None,
    ) -> None:
        base = spec or PROVIDER_SPECS[name]
        overrides = {}
        if max_input_tokens is not None:
            overrides["max_input_tokens"] = max_input_tokens
        if model is not None:
            overrides["default_model"] = model
        super().__init__(
            dataclasses.replace(base, **overrides) if overrides else base,
            sleep=lambda _seconds: None,  # never actually back off in tests
        )
        self.responses = list(responses)
        self.prompts: List[str] = []

    def _generate_once(self, prompt: str, max_output_tokens: int) -> str:
        self.prompts.append(prompt)
        if not self.responses:
            raise AssertionError(f"ScriptedProvider({self.name}) ran out of scripted responses")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def verify(self, *, probe: bool = False) -> Tuple[bool, str]:
        return True, "scripted"

    @property
    def calls(self) -> int:
        return len(self.prompts)
