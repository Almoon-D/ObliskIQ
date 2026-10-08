"""
LLMAnalyst — the provider-independent half of the AI analyst.

Everything that must be identical no matter which model answers lives here:

* Prompt composition (v3.3 modular composer, sized to the PROVIDER's input window).
* Extracting JSON from model output (reasoning blocks, markdown fences, prose).
* Contract validation with the retry-once-with-the-error protocol — Phase 4
  (decision contract v1.0, since v6.12.0) and now Phase 2 as well, so a fallback
  model is held to exactly the same standard as the primary.

Everything provider-specific (SDK, HTTP, error vocabulary, retry/backoff on rate
limits) lives behind ``providers.LLMProvider``. ``analyst_router`` chains analysts
across providers; ``gemini_integration.GeminiAnalyst`` is the single-provider
convenience wrapper kept for import compatibility.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Callable, Dict, Optional

from modules.analysis.providers import LLMProvider
from modules.exceptions import ModelError
from modules.models.schemas import (
    PHASE2_SCHEMA_VERSION,
    PHASE4_SCHEMA_VERSION,
    contract_decision_to_dict,
    parse_phase2_response,
    parse_phase4_response,
)
from modules.utils.hybrid_formatter import HybridFormatter
from modules.utils.prompt_composer import PromptComposer
from modules.utils.token_budget import PromptBudgetError, estimate_tokens

logger = logging.getLogger(__name__)

# Reasoning blocks some models inline in the answer (the Gemini API itself returns
# thoughts as separate parts, but open models on OpenRouter/NVIDIA/Groq often use tags).
_REASONING_BLOCK = re.compile(r"<(think|thinking|reasoning)>.*?</\1>", re.DOTALL | re.IGNORECASE)
_UNCLOSED_REASONING = re.compile(r"^\s*<(think|thinking|reasoning)>.*?(?=\{)", re.DOTALL | re.IGNORECASE)
_FENCE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.DOTALL)


def _first_balanced_object(text: str) -> Optional[str]:
    """The first top-level ``{...}`` in ``text`` (string-aware brace matching)."""
    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return text[start : index + 1]
        start = text.find("{", start + 1)
    return None


def clean_model_json(text: str) -> str:
    """
    Reduce raw model output to the JSON object it contains.

    Handles, in order: inline reasoning blocks, markdown fences, and leading /
    trailing prose around a single JSON object. Returns the input stripped when
    no object can be located, so the caller's JSON parse produces the real error.
    """
    cleaned = (text or "").strip()
    cleaned = _REASONING_BLOCK.sub("", cleaned)
    cleaned = _UNCLOSED_REASONING.sub("", cleaned).strip()

    fenced = _FENCE.search(cleaned)
    if fenced:
        cleaned = fenced.group(1).strip()
    else:
        cleaned = cleaned.replace("```json", "").replace("```", "").strip()

    if not cleaned.startswith("{"):
        extracted = _first_balanced_object(cleaned)
        if extracted:
            return extracted.strip()
    return cleaned


class LLMAnalyst:
    """AI analyst bound to ONE provider (see AnalystRouter for failover)."""

    def __init__(
        self,
        provider: LLMProvider,
        *,
        use_v33_prompts: bool = True,
        prompt_composer: Optional[PromptComposer] = None,
    ) -> None:
        self.provider = provider
        self.use_v33_prompts = use_v33_prompts
        self.prompt_composer = (
            (prompt_composer or PromptComposer()) if use_v33_prompts else None
        )

    # -- identity ------------------------------------------------------------

    @property
    def model_name(self) -> str:
        """Plain model ID (``GeminiAnalyst.model_name`` has always meant this)."""
        return self.provider.model_name

    @property
    def label(self) -> str:
        """``provider:model`` for scoreboard attribution."""
        return self.provider.label

    @property
    def token_ceiling(self) -> int:
        """This provider's input window (drives prompt pruning)."""
        return self.provider.spec.max_input_tokens

    # -- shared plumbing -----------------------------------------------------

    def _request_json(self, prompt: str) -> str:
        """One model call, reduced to the JSON text inside the response."""
        return clean_model_json(self.provider.generate(prompt))

    def run_contract(self, prompt: str, parse: Callable[[str], Any], *, what: str) -> Any:
        """
        One self-contained prompt whose answer must satisfy ``parse`` (which raises ``ValueError``).

        The same protocol as Phase 2/4 — validate, retry ONCE with the error appended, then fail —
        for callers that bring their own prompt and contract (the weekly forecast panel). A prompt that
        cannot fit this provider's window raises ``PromptBudgetError`` before any call is made.
        """
        needed = estimate_tokens(prompt)
        if needed > self.token_ceiling:
            raise PromptBudgetError(
                f"{what} prompt needs ~{needed:,} tokens; {self.label} accepts {self.token_ceiling:,}"
            )
        response_text = self._request_json(prompt)
        try:
            return parse(response_text)
        except ValueError as first_error:
            logger.warning("%s response rejected: %s — retrying once with the error appended", what, first_error)
            retry_prompt = (
                f"{prompt}\n\n---\n\nPREVIOUS RESPONSE REJECTED: {first_error}\n"
                "Return ONLY corrected JSON matching the schema above. No markdown fences, no prose."
            )
            try:
                return parse(self._request_json(retry_prompt))
            except ValueError as retry_error:
                raise ModelError(
                    f"{what} response failed validation after one retry: {retry_error}",
                    model_name=self.label,
                ) from retry_error

    # -- Phase 2 -------------------------------------------------------------

    def analyze_intelligence_report(
        self, report_data: Dict[str, Any], master_prompt_path: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Phase 2: analyze Phase 1 intelligence and propose tickers.

        Returns the NORMALIZED analysis dict (``suggested_tickers`` as strings,
        top-level ``reflexivity_intensity``, per-ticker ``ticker_details``).
        Raises ModelError if the response violates the Phase-2 contract after one
        retry with the validation error appended.
        """
        if self.use_v33_prompts and self.prompt_composer:
            logger.info("Composing Phase 2 prompt (v3.3 modular, ceiling %s tokens)",
                        f"{self.token_ceiling:,}")
            prompt = self.prompt_composer.compose_phase2_prompt(
                report_data, token_ceiling=self.token_ceiling
            )
        else:
            prompt = self._legacy_phase2_prompt(report_data, master_prompt_path)

        logger.info("Sending intelligence report to %s ...", self.label)
        response_text = self._request_json(prompt)

        try:
            result = parse_phase2_response(response_text)
        except ValueError as first_error:
            logger.warning(
                "Phase 2 response failed contract v%s: %s — retrying once with the error appended",
                PHASE2_SCHEMA_VERSION, first_error,
            )
            retry_prompt = (
                f"{prompt}\n\n---\n\n"
                f"PREVIOUS RESPONSE REJECTED: {first_error}\n"
                "Return ONLY corrected JSON matching the Phase 2 schema: a top-level "
                '"market_regime" string and a non-empty "suggested_tickers" array. '
                "No markdown fences, no prose outside the JSON object."
            )
            response_text = self._request_json(retry_prompt)
            try:
                result = parse_phase2_response(response_text)
            except ValueError as retry_error:
                raise ModelError(
                    "Phase 2 AI response failed the proposal contract after one retry "
                    f"(schema v{PHASE2_SCHEMA_VERSION}): {retry_error}",
                    model_name=self.label,
                ) from retry_error

        self._log_reflexivity(result)
        logger.info("Phase 2 analysis complete: %d tickers suggested", len(result["suggested_tickers"]))
        return result

    @staticmethod
    def _log_reflexivity(result: Dict[str, Any]) -> None:
        intensity = float(result.get("reflexivity_intensity", 0.0) or 0.0)
        if intensity >= 0.6:
            logger.info("REFLEXIVITY DETECTED: intensity %.2f (Soros mode); components: %s",
                        intensity, result.get("component_scores"))
        elif intensity >= 0.3:
            logger.info("Weak reflexivity: intensity %.2f (monitor)", intensity)
        else:
            logger.info("Normal regime: reflexivity intensity %.2f (Simons/Dalio mode)", intensity)

    # -- Phase 4 -------------------------------------------------------------

    def analyze_ticker_details(
        self,
        ticker_data: dict,
        master_prompt_path: Optional[str] = None,
        phase2_analysis: Optional[dict] = None,
    ) -> dict:
        """
        Phase 4: analyze Phase 3 ticker data and return trading decisions.

        Hard-validated against the Phase-4 decision contract; a malformed response
        is rejected and retried ONCE with the validation error appended, then
        raises ModelError (the run is marked failed). Decisions parse from JSON only.
        """
        if self.use_v33_prompts and self.prompt_composer:
            if phase2_analysis:
                ticker_data["_phase2_reflexivity"] = {
                    "intensity": phase2_analysis.get("reflexivity_intensity", 0.0),
                    "components": phase2_analysis.get("component_scores", {}),
                    "consensus_belief": phase2_analysis.get("consensus_belief", ""),
                    "consensus_vulnerability": phase2_analysis.get("consensus_vulnerability", ""),
                }
            prompt = self.prompt_composer.compose_phase4_prompt(
                ticker_data, token_ceiling=self.token_ceiling
            )
        else:
            prompt = self._legacy_phase4_prompt(ticker_data, master_prompt_path)

        logger.info("Sending ticker analysis to %s ...", self.label)
        response_text = self._request_json(prompt)

        try:
            contract = parse_phase4_response(response_text)
        except ValueError as first_error:
            logger.warning(
                "Phase 4 response failed contract v%s: %s — retrying once with the error appended",
                PHASE4_SCHEMA_VERSION, first_error,
            )
            retry_prompt = (
                f"{prompt}\n\n---\n\n"
                f"PREVIOUS RESPONSE REJECTED: {first_error}\n"
                f"Return ONLY corrected JSON matching the Phase 4 action schema "
                f'(schema_version "{PHASE4_SCHEMA_VERSION}"). '
                "No markdown fences, no prose outside the JSON object."
            )
            response_text = self._request_json(retry_prompt)
            try:
                contract = parse_phase4_response(response_text)
            except ValueError as retry_error:
                raise ModelError(
                    "Phase 4 AI response failed decision-contract validation "
                    f"after one retry (schema v{PHASE4_SCHEMA_VERSION}): {retry_error}",
                    model_name=self.label,
                ) from retry_error

        # Bridge validated contract decisions into the internal decisions-dict
        # shape (conviction 0-1 -> 0-10, size_pct -> position_size_pct/fraction);
        # prose envelope keys pass through untouched for the human log.
        result: Dict[str, Any] = contract.model_dump(mode="json")
        result["portfolio_decisions"] = [
            contract_decision_to_dict(d) for d in contract.portfolio_decisions
        ]
        logger.info(
            "Trading decisions complete: %d recommendations (contract v%s)",
            len(result["portfolio_decisions"]), PHASE4_SCHEMA_VERSION,
        )
        return result

    # -- legacy v3.0 prompts (use_v33_prompts=False) ---------------------------

    @staticmethod
    def _read_master_prompt(master_prompt_path: Optional[str]) -> str:
        if master_prompt_path is None:
            raise ValueError("master_prompt_path required when not using v3.3 prompts")
        if not os.path.exists(master_prompt_path):
            raise FileNotFoundError(f"MasterPrompt not found: {master_prompt_path}")
        with open(master_prompt_path, "r", encoding="utf-8") as handle:
            return handle.read()

    def _legacy_phase2_prompt(self, report_data: Dict[str, Any], master_prompt_path: Optional[str]) -> str:
        master_prompt = self._read_master_prompt(master_prompt_path)
        formatted_intelligence = HybridFormatter.format_phase1_intelligence(report_data)
        return f"""{master_prompt}

---

# PHASE 1 INTELLIGENCE REPORT TO ANALYZE

{formatted_intelligence}

---

# YOUR TASK

Analyze the above intelligence report with extreme depth and rigor. Consider:

1. **Macroeconomic Regime**: Growth, inflation, liquidity, credit conditions
2. **Market Sentiment**: Fear/greed, positioning, momentum
3. **Geopolitical Risks**: Policy changes, elections, conflicts
4. **Sector Dynamics**: Which sectors benefit from current conditions?
5. **Cross-Asset Correlations**: Stocks, bonds, commodities, currencies

Based on this multi-dimensional analysis, suggest 10-20 specific tickers (stocks, ETFs, bonds) that offer the best risk/reward given the current regime.

**CRITICAL: Respond with ONLY valid JSON in this exact format:**

{{
  "market_regime": "bull/bear/choppy/stagflation/recession",
  "regime_confidence": 1-10,
  "macro_summary": "2-3 sentence synthesis of key macro drivers",
  "key_themes": ["theme1", "theme2", "theme3"],
  "suggested_tickers": ["TICKER1", "TICKER2", ...],
  "ticker_rationale": {{
    "TICKER1": "Why this ticker fits the regime",
    "TICKER2": "Why this ticker fits the regime"
  }},
  "sector_allocation": {{
    "Technology": 25,
    "Healthcare": 15,
    "Energy": 10
  }},
  "risk_level": "low/medium/high",
  "hedging_suggestions": ["How to hedge current positions"],
  "regime_triggers": ["What would change the current regime"]
}}

Output ONLY the JSON. No markdown formatting, no code blocks, no explanations outside JSON.
"""

    def _legacy_phase4_prompt(self, ticker_data: dict, master_prompt_path: Optional[str]) -> str:
        master_prompt = self._read_master_prompt(master_prompt_path)
        formatted_ticker_analysis = HybridFormatter.format_phase3_ticker_analysis(ticker_data)
        return f"""{master_prompt}

---

# PHASE 3 TICKER ANALYSIS DATA

{formatted_ticker_analysis}

---

# YOUR TASK

You now have deep fundamental, technical, and options data for each ticker. Provide specific, actionable trading decisions.

**CRITICAL: Respond with ONLY valid JSON matching the Phase 4 action schema
(schema_version "{PHASE4_SCHEMA_VERSION}"): a "portfolio_decisions" array whose rows carry
action, ticker, conviction (0-1), size_pct, thesis, win_probability, payoff_ratio and, for
every BUY, at least one invalidation_trigger.**

Output ONLY the JSON. No markdown formatting, no code blocks, no explanations outside JSON.
"""


__all__ = ["LLMAnalyst", "clean_model_json"]
