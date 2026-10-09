"""
AnalystRouter — ordered failover across AI providers.

The operator picks the ORDER (``AI_PROVIDER_ORDER`` / panel ``[ai] provider_order``);
the router tries each provider in turn for each phase and returns the first answer
that survives the full contract validation. Phases 2 and 4 route independently, so
a Gemini rate limit in Phase 2 does not force Phase 4 onto a fallback.

What counts as "this provider cannot serve the call, try the next":

* not configured (no API key) .................... skipped, no network
* previously found dead this run (bad key / model ID) .. skipped, no network
* circuit open (repeated recent failures) ........... skipped, no network
* the prompt cannot fit its input window ............ skipped (never called, so it
  does NOT count against the provider's circuit)
* HTTP/transport/rate-limit failure after its own bounded retry
* empty/blocked response
* a response that violates the Phase 2 / Phase 4 contract even after the one
  retry-with-the-error (a different model may do better)

If every provider is skipped or fails, ``AllProvidersFailedError`` lists exactly why
for each — the run fails loudly (and notifies); it never fabricates decisions.

Every result carries ``ai_provider`` (provider, model, degraded flag, attempt log).
``degraded`` is True when the answering provider's input window is below
``FULL_CONTEXT_MIN_TOKENS``: it saw a heavily pruned briefing, so the orchestrator
lets such a run hold or exit positions but never open new ones.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import Any, Callable, Dict, List, Optional

from modules.analysis.llm_analyst import LLMAnalyst
from modules.analysis.providers import (
    LLMProvider,
    ModelNotFoundError,
    ProviderError,
    ProviderUnavailableError,
    build_provider,
)
from modules.config.ai_providers import PROVIDER_SPECS, resolve_provider_order
from modules.exceptions import ModelError
from modules.utils.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitBreakerManager,
)
from modules.utils.prompt_composer import PromptComposer
from modules.utils.token_budget import PromptBudgetError

logger = logging.getLogger(__name__)

# Two consecutive failures open a provider's circuit for 15 minutes: long enough that
# Phase 4 does not re-hammer a provider that just rate-limited Phase 2, short enough
# that the NEXT scheduled run (hours later, a new process anyway) starts clean.
BREAKER_CONFIG = CircuitBreakerConfig(failure_threshold=2, timeout_seconds=900.0)


@dataclass
class ProviderAttempt:
    """One provider's turn in a routed call."""

    provider: str
    model: str
    outcome: str  # "ok" | "skipped" | "failed"
    reason: str = ""


class AllProvidersFailedError(ModelError):
    """Every provider in the chain was skipped or failed for this phase."""

    def __init__(self, phase: str, attempts: List[ProviderAttempt]) -> None:
        self.phase = phase
        self.attempts = attempts
        detail = "; ".join(f"{a.provider} {a.outcome}: {a.reason}" for a in attempts) or "no providers"
        super().__init__(
            f"All AI providers failed for {phase}: {detail}",
            model_name="router",
        )


class AnalystRouter:
    """Drop-in analyst (``analyze_intelligence_report`` / ``analyze_ticker_details``)
    that fails over across providers in the operator's order."""

    def __init__(
        self,
        *,
        order: Optional[List[str]] = None,
        providers: Optional[Dict[str, LLMProvider]] = None,
        use_v33_prompts: bool = True,
        prompt_composer: Optional[PromptComposer] = None,
        breakers: Optional[CircuitBreakerManager] = None,
    ) -> None:
        self.order: List[str] = list(order) if order is not None else resolve_provider_order()
        self.use_v33_prompts = use_v33_prompts
        self._composer = (prompt_composer or PromptComposer()) if use_v33_prompts else None
        self._providers: Dict[str, LLMProvider] = dict(providers or {})
        # Names handed in by the caller (tests / custom wiring) are assumed usable;
        # providers built lazily from the registry are usable only if their key is set.
        self._injected = frozenset(self._providers)
        self._breakers = breakers or CircuitBreakerManager(default_config=BREAKER_CONFIG)
        self._dead: Dict[str, str] = {}  # provider -> permanent reason, for the rest of this run
        self._last_label: Optional[str] = None
        self.attempt_log: List[ProviderAttempt] = []

        configured = [n for n in self.order if self._is_configured(n)]
        if not configured:
            raise ValueError(
                "No AI provider is configured. Set at least one of: "
                + ", ".join(f"{PROVIDER_SPECS[n].api_key_env} ({n})" for n in self.order)
                + ". The free Gemini key is at https://aistudio.google.com/apikey"
            )
        logger.info(
            "AI provider chain: %s (not configured, will be skipped: %s)",
            " -> ".join(f"{n}:{self._provider(n).model_name}" for n in self.order),
            ", ".join(n for n in self.order if n not in configured) or "none",
        )

    # -- providers -------------------------------------------------------------

    def _provider(self, name: str) -> LLMProvider:
        if name not in self._providers:
            self._providers[name] = build_provider(PROVIDER_SPECS[name])
        return self._providers[name]

    def _is_configured(self, name: str) -> bool:
        if name in self._injected:
            return True
        return bool(PROVIDER_SPECS[name].api_key())

    def _breaker(self, name: str, scope: str = "ai") -> CircuitBreaker:
        return self._breakers.get(f"{scope}:{name}", BREAKER_CONFIG)

    # -- identity (read by the orchestrator for scoreboard attribution) ----------

    @property
    def model_name(self) -> str:
        """``provider:model`` of the last answering provider, else the first configured."""
        if self._last_label:
            return self._last_label
        for name in self.order:
            if self._is_configured(name):
                return self._provider(name).label
        return "unconfigured"

    # -- health (scripts/preflight.py) -------------------------------------------

    def verify_all(self, *, probe: bool = False) -> List[Dict[str, Any]]:
        """Per-provider status in chain order. Never raises, never calls an
        unconfigured provider."""
        rows: List[Dict[str, Any]] = []
        for name in self.order:
            spec = PROVIDER_SPECS[name]
            provider = self._provider(name)
            if not self._is_configured(name):
                rows.append({"provider": name, "model": provider.model_name, "configured": False,
                             "ok": False, "detail": f"{spec.api_key_env} not set",
                             "full_context": spec.full_context})
                continue
            ok, detail = provider.verify(probe=probe)
            rows.append({"provider": name, "model": provider.model_name, "configured": True,
                         "ok": ok, "detail": detail, "full_context": spec.full_context})
        return rows

    # -- routing -----------------------------------------------------------------

    def _route(
        self, phase: str, call: Callable[[LLMAnalyst], Dict[str, Any]], *, breaker_scope: str = "ai"
    ) -> Dict[str, Any]:
        """Route one call down the chain. ``breaker_scope`` keeps an optional workload's failures
        (the weekly forecast panel) from opening the circuit the trading phases depend on."""
        attempts: List[ProviderAttempt] = []
        for name in self.order:
            spec = PROVIDER_SPECS[name]
            provider = self._provider(name)
            model = provider.model_name

            if not self._is_configured(name):
                attempts.append(ProviderAttempt(name, model, "skipped", f"{spec.api_key_env} not set"))
                continue
            if name in self._dead:
                attempts.append(ProviderAttempt(name, model, "skipped", f"unusable this run: {self._dead[name]}"))
                continue
            breaker = self._breaker(name, breaker_scope)
            if not breaker.allow_request():
                attempts.append(ProviderAttempt(name, model, "skipped", "circuit open after repeated recent failures"))
                continue

            analyst = LLMAnalyst(provider, use_v33_prompts=self.use_v33_prompts, prompt_composer=self._composer)
            try:
                result = call(analyst)
            except PromptBudgetError as exc:
                # The provider was never called: not its fault, no breaker penalty.
                attempts.append(ProviderAttempt(name, model, "skipped", str(exc)))
                logger.warning("%s skipped for %s: %s", name, phase, exc)
                continue
            except (ProviderError, ModelError, ValueError) as exc:
                reason = getattr(exc, "reason", None) or str(exc)
                if isinstance(exc, (ProviderUnavailableError, ModelNotFoundError)):
                    self._dead[name] = reason
                breaker.record_failure()
                attempts.append(ProviderAttempt(name, model, "failed", reason[:300]))
                logger.warning("%s failed for %s: %s — trying next provider", name, phase, reason[:300])
                continue

            breaker.record_success()
            attempts.append(ProviderAttempt(name, model, "ok"))
            self._last_label = provider.label
            self.attempt_log = attempts
            result["ai_provider"] = {
                "provider": name,
                "model": model,
                "label": provider.label,
                # the provider's OWN spec is authoritative (it is what the transport and the
                # prompt budget use); the registry spec is only the default it was built from
                "degraded": not provider.spec.full_context,
                "attempts": [asdict(a) for a in attempts],
            }
            if len(attempts) > 1:
                logger.warning("%s answered after failover: %s", phase,
                               "; ".join(f"{a.provider}={a.outcome}" for a in attempts))
            return result

        self.attempt_log = attempts
        raise AllProvidersFailedError(phase, attempts)

    # -- analyst interface ---------------------------------------------------------

    def analyze_intelligence_report(
        self, report_data: Dict[str, Any], master_prompt_path: Optional[str] = None
    ) -> Dict[str, Any]:
        """Phase 2 across the provider chain."""
        return self._route(
            "Phase 2", lambda analyst: analyst.analyze_intelligence_report(report_data, master_prompt_path)
        )

    def analyze_ticker_details(
        self,
        ticker_data: dict,
        master_prompt_path: Optional[str] = None,
        phase2_analysis: Optional[dict] = None,
    ) -> dict:
        """Phase 4 across the provider chain."""
        return self._route(
            "Phase 4",
            lambda analyst: analyst.analyze_ticker_details(ticker_data, master_prompt_path, phase2_analysis),
        )

    def run_contract(self, what: str, prompt: str, parse: Callable[[str], Any]) -> tuple:
        """A self-contained prompt + validator across the provider chain (the weekly forecast panel).

        Returns ``(parsed, provenance)`` where provenance names the provider/model that answered.
        Raises ``AllProvidersFailedError`` when no provider could serve it. Uses its OWN circuit-breaker
        scope: an optional workload hitting a rate limit must never block Phase 2/4 of a trading run.
        """
        routed = self._route(
            what, lambda analyst: {"value": analyst.run_contract(prompt, parse, what=what)}, breaker_scope="panel"
        )
        return routed["value"], routed["ai_provider"]
