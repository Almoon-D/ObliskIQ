"""
AI provider registry — the frozen, in-code description of every model provider
the analyst can fail over to.

Design (see docs/AI_PROVIDERS.md):

* The OPERATOR controls exactly two things, both through
  ``config/operator_panel.toml`` -> env vars: the ORDER providers are tried in
  (``AI_PROVIDER_ORDER``, a comma-separated list) and which MODEL each uses
  (``GEMINI_MODEL`` / ``OPENROUTER_MODEL`` / ``NVIDIA_MODEL`` / ``GROQ_MODEL``).
  To disable a provider, leave it out of the order.
* Everything that can change what the AI is allowed to see or do — token
  ceilings, JSON mode, whether a provider counts as "full context" — lives HERE,
  in code, reviewed like any other risk-relevant change (the panel loader bans
  token ceilings on purpose).
* Secrets are never in this file: only the NAME of the env var holding each key.

Free-tier reality (checked 2026-10; re-verify before raising a ceiling):

* Gemini: Pro models are paid-only since April 2026; Flash models are free.
  Limits are per PROJECT, not per key, and are only published in AI Studio.
* OpenRouter ``:free`` models: 20 req/min, 50 req/day (1,000/day once >= $10 of
  credit has ever been purchased).
* NVIDIA NIM: ~40 req/min, no published token cap.
* Groq free: ~8K tokens/minute and 200K/day on text models, so it cannot carry the
  full Phase 2/4 prompts (persona + schema alone exceed it). It stays in the
  registry because the operator asked for it and a paid Groq plan lifts the
  limit — until then the router skips it with an explicit reason instead of
  sending a request that would 413.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from modules.config.settings import DEFAULT_GEMINI_MODEL
from modules.exceptions import ConfigurationError

# A provider whose input ceiling is below this serves a DEGRADED view of the
# briefing (low-priority sections pruned hard). Runs it serves may hold or exit
# positions but never open new ones (enforced in the orchestrator).
FULL_CONTEXT_MIN_TOKENS = 100_000

ENV_PROVIDER_ORDER = "AI_PROVIDER_ORDER"
DEFAULT_PROVIDER_ORDER: Tuple[str, ...] = ("gemini", "openrouter", "nvidia", "groq")


@dataclass(frozen=True)
class ProviderSpec:
    """Immutable description of one provider (never holds a secret)."""

    name: str
    kind: str  # "gemini" (google-genai SDK) | "openai_compat" (chat/completions)
    api_key_env: str
    model_env: str
    default_model: str
    max_input_tokens: int
    max_output_tokens: int
    base_url: str = ""
    json_mode: bool = True
    # None = provider default. Google: keep Gemini 3 at its default (1.0).
    temperature: Optional[float] = None
    # Gemini 3 only: minimal | low | medium | high.
    thinking_level: Optional[str] = None
    request_timeout_s: float = 300.0

    @property
    def full_context(self) -> bool:
        """True when this provider can carry the full-size briefing."""
        return self.max_input_tokens >= FULL_CONTEXT_MIN_TOKENS

    def model(self) -> str:
        """Configured model: env override, else the registry default."""
        return os.getenv(self.model_env, "").strip() or self.default_model

    def api_key(self) -> str:
        """The key from the environment ('' when unset)."""
        return os.getenv(self.api_key_env, "").strip()


# NOTE on defaults: free-model IDs on OpenRouter/NVIDIA rotate. The router verifies
# the configured ID at startup (scripts/preflight.py does the same) and fails over
# with an explicit "model not found" reason, so a rotated ID degrades to the next
# provider instead of breaking the run; change it via the *_MODEL env var / panel.
PROVIDER_SPECS: Dict[str, ProviderSpec] = {
    "gemini": ProviderSpec(
        name="gemini",
        kind="gemini",
        api_key_env="GEMINI_API_KEY",
        model_env="GEMINI_MODEL",
        default_model=DEFAULT_GEMINI_MODEL,
        max_input_tokens=250_000,  # the frozen per-call backstop (HandoffBudgetConfig)
        max_output_tokens=32_768,  # thinking tokens share this budget with the JSON
        thinking_level="high",
        request_timeout_s=300.0,
    ),
    "openrouter": ProviderSpec(
        name="openrouter",
        kind="openai_compat",
        api_key_env="OPENROUTER_API_KEY",
        model_env="OPENROUTER_MODEL",
        default_model="nvidia/nemotron-3-super-120b-a12b:free",  # 262k ctx, JSON-capable
        base_url="https://openrouter.ai/api/v1",
        max_input_tokens=200_000,
        # A reasoning model: thinking tokens share this budget with the JSON (run #188: empty content,
        # finish_reason=length at 16_384; run #187: Phase 4 JSON cut mid-string).
        max_output_tokens=32_768,
        temperature=0.2,
        request_timeout_s=300.0,
    ),
    "nvidia": ProviderSpec(
        name="nvidia",
        kind="openai_compat",
        api_key_env="NVIDIA_API_KEY",
        model_env="NVIDIA_MODEL",
        # nvidia/nemotron-3-super-120b-a12b was retired on 2026-10-03 (HTTP 410). A non-reasoning model: a
        # reasoning one spends max_tokens on thinking and can return empty content (OpenRouter, run #188).
        default_model="nvidia/llama-3.1-nemotron-70b-instruct",
        base_url="https://integrate.api.nvidia.com/v1",
        max_input_tokens=100_000,  # conservative; raise only after measuring the model's real window
        max_output_tokens=32_768,
        temperature=0.2,
        request_timeout_s=300.0,
    ),
    "groq": ProviderSpec(
        name="groq",
        kind="openai_compat",
        api_key_env="GROQ_API_KEY",
        model_env="GROQ_MODEL",
        default_model="openai/gpt-oss-120b",
        base_url="https://api.groq.com/openai/v1",
        max_input_tokens=6_000,  # free tier: ~8K TPM shared by input AND output
        max_output_tokens=2_048,
        temperature=0.2,
        request_timeout_s=120.0,
    ),
}


def parse_provider_order(raw: Optional[str]) -> List[str]:
    """
    Parse a comma-separated provider order, validating every name.

    Blank / unset -> the default order. Whitespace and case are ignored. Unknown
    or duplicate names raise ConfigurationError (fail loud: a typo here would
    otherwise silently drop a provider from the failover chain).
    """
    if raw is None or not raw.strip():
        return list(DEFAULT_PROVIDER_ORDER)

    order: List[str] = []
    for token in raw.split(","):
        name = token.strip().lower()
        if not name:
            continue
        if name not in PROVIDER_SPECS:
            raise ConfigurationError(
                f"{ENV_PROVIDER_ORDER} names an unknown AI provider '{name}'. "
                f"Known providers: {sorted(PROVIDER_SPECS)}.",
                config_key=ENV_PROVIDER_ORDER,
                config_value=raw,
            )
        if name in order:
            raise ConfigurationError(
                f"{ENV_PROVIDER_ORDER} lists provider '{name}' more than once.",
                config_key=ENV_PROVIDER_ORDER,
                config_value=raw,
            )
        order.append(name)

    if not order:
        raise ConfigurationError(
            f"{ENV_PROVIDER_ORDER} is set but names no providers.",
            config_key=ENV_PROVIDER_ORDER,
            config_value=raw,
        )
    return order


def resolve_provider_order() -> List[str]:
    """The operator's provider order (env / panel), validated."""
    return parse_provider_order(os.getenv(ENV_PROVIDER_ORDER))
