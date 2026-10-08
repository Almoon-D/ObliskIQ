"""
Tests for AnalystRouter — ordered failover across AI providers.

Every scenario drives the REAL router + REAL LLMAnalyst + REAL contract validation;
only the transports are scripted. The operator-facing guarantees under test:

* providers are tried in the configured ORDER, and reordering changes who answers;
* if one fails (rate limit, bad model, junk reply, contract violation) the next one
  answers, and the answer is attributed to the provider that actually gave it;
* skipped providers cost no network call and always say why;
* when everything fails the error lists every provider's reason (no fabricated output).
"""

import json
from unittest.mock import MagicMock

import pytest

from llm_fakes import ScriptedProvider
from modules.analysis.analyst_router import (
    AllProvidersFailedError,
    AnalystRouter,
)
from modules.analysis.providers import (
    ModelNotFoundError,
    ProviderCallError,
    ProviderUnavailableError,
)
from modules.config.ai_providers import PROVIDER_SPECS
from modules.exceptions import ModelError
from modules.utils.token_budget import PromptBudgetError

pytestmark = pytest.mark.unit

PHASE2_OK = json.dumps(
    {"market_regime": "bull", "regime_confidence": 7, "suggested_tickers": ["EWJ", "INDA"]}
)
PHASE4_OK = json.dumps(
    {
        "schema_version": "1.0",
        "portfolio_decisions": [
            {
                "ticker": "EWJ", "action": "BUY", "conviction": 0.7, "size_pct": 8.0,
                "thesis": "Governance reform re-rating continues across Japanese equities.",
                "invalidation_triggers": [
                    {"trigger_id": "t1", "type": "price_below", "threshold": 60.0, "description": "close below 60"}
                ],
                "win_probability": 0.58, "payoff_ratio": 2.0, "entry_price": 70.0,
            }
        ],
    }
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("GEMINI_API_KEY", "OPENROUTER_API_KEY", "NVIDIA_API_KEY", "GROQ_API_KEY",
                "GEMINI_MODEL", "OPENROUTER_MODEL", "NVIDIA_MODEL", "GROQ_MODEL", "AI_PROVIDER_ORDER"):
        monkeypatch.delenv(var, raising=False)


def composer():
    pc = MagicMock()
    pc.compose_phase2_prompt.return_value = "P2"
    pc.compose_phase4_prompt.return_value = "P4"
    return pc


def router(order, providers, **kwargs):
    return AnalystRouter(order=order, providers=providers, prompt_composer=composer(), **kwargs)


def rate_limited(name="gemini"):
    return ProviderCallError(name, "rate limited (429)", retryable=False, status=429)


class TestOrderAndFailover:
    def test_first_provider_answers_when_healthy(self):
        gemini, nvidia = ScriptedProvider([PHASE2_OK]), ScriptedProvider([PHASE2_OK], name="nvidia")
        r = router(["gemini", "nvidia"], {"gemini": gemini, "nvidia": nvidia})
        result = r.analyze_intelligence_report({})
        assert result["suggested_tickers"] == ["EWJ", "INDA"]
        assert (gemini.calls, nvidia.calls) == (1, 0)
        assert result["ai_provider"]["provider"] == "gemini"
        assert result["ai_provider"]["degraded"] is False

    def test_failure_jumps_to_the_next_provider(self):
        gemini = ScriptedProvider([rate_limited()])
        openrouter = ScriptedProvider([PHASE2_OK], name="openrouter")
        r = router(["gemini", "openrouter"], {"gemini": gemini, "openrouter": openrouter})
        result = r.analyze_intelligence_report({})
        assert result["ai_provider"]["provider"] == "openrouter"
        attempts = result["ai_provider"]["attempts"]
        assert [(a["provider"], a["outcome"]) for a in attempts] == [("gemini", "failed"), ("openrouter", "ok")]
        assert "rate limited" in attempts[0]["reason"]

    def test_reordering_the_chain_changes_who_answers(self):
        def build(order):
            return router(order, {
                "gemini": ScriptedProvider([PHASE2_OK]),
                "nvidia": ScriptedProvider([PHASE2_OK], name="nvidia"),
            })
        assert build(["gemini", "nvidia"]).analyze_intelligence_report({})["ai_provider"]["provider"] == "gemini"
        assert build(["nvidia", "gemini"]).analyze_intelligence_report({})["ai_provider"]["provider"] == "nvidia"

    def test_a_provider_left_out_of_the_order_is_never_called(self):
        gemini = ScriptedProvider([rate_limited()])
        groq = ScriptedProvider([PHASE2_OK], name="groq")
        with pytest.raises(AllProvidersFailedError):
            router(["gemini"], {"gemini": gemini, "groq": groq}).analyze_intelligence_report({})
        assert groq.calls == 0

    def test_three_deep_failover(self):
        providers = {
            "gemini": ScriptedProvider([rate_limited()]),
            "openrouter": ScriptedProvider([ProviderCallError("openrouter", "server error 503", status=503)], name="openrouter"),
            "nvidia": ScriptedProvider([PHASE2_OK], name="nvidia"),
        }
        result = router(["gemini", "openrouter", "nvidia"], providers).analyze_intelligence_report({})
        assert result["ai_provider"]["provider"] == "nvidia"

    def test_contract_violation_on_the_primary_fails_over(self):
        """Primary returns junk twice (retry exhausted) -> ModelError -> next provider."""
        gemini = ScriptedProvider(["not json", "still not json"])
        nvidia = ScriptedProvider([PHASE2_OK], name="nvidia")
        result = router(["gemini", "nvidia"], {"gemini": gemini, "nvidia": nvidia}).analyze_intelligence_report({})
        assert result["ai_provider"]["provider"] == "nvidia"
        assert gemini.calls == 2  # its own retry-with-the-error happened first

    def test_phase4_routes_independently_of_phase2(self):
        gemini = ScriptedProvider([PHASE2_OK, rate_limited()])  # fine in Phase 2, limited in Phase 4
        nvidia = ScriptedProvider([PHASE4_OK], name="nvidia")
        r = router(["gemini", "nvidia"], {"gemini": gemini, "nvidia": nvidia})
        assert r.analyze_intelligence_report({})["ai_provider"]["provider"] == "gemini"
        decisions = r.analyze_ticker_details({"EWJ": {}})
        assert decisions["ai_provider"]["provider"] == "nvidia"
        assert decisions["portfolio_decisions"][0]["ticker"] == "EWJ"


class TestSkipsCostNothingAndSayWhy:
    def test_unconfigured_provider_is_skipped_with_its_env_var_named(self):
        # Not injected + no key in the env -> "not configured"; the injected one answers.
        nvidia = ScriptedProvider([PHASE2_OK], name="nvidia")
        r = router(["gemini", "nvidia"], {"nvidia": nvidia})
        result = r.analyze_intelligence_report({})
        first = result["ai_provider"]["attempts"][0]
        assert (first["provider"], first["outcome"]) == ("gemini", "skipped")
        assert "GEMINI_API_KEY" in first["reason"]

    def test_bad_model_id_is_dead_for_the_rest_of_the_run(self):
        gemini = ScriptedProvider([ModelNotFoundError("gemini", "model 'x' not found", status=404)])
        nvidia = ScriptedProvider([PHASE2_OK, PHASE4_OK], name="nvidia")
        r = router(["gemini", "nvidia"], {"gemini": gemini, "nvidia": nvidia})
        r.analyze_intelligence_report({})
        phase4 = r.analyze_ticker_details({"EWJ": {}})
        assert gemini.calls == 1  # never retried in Phase 4
        skipped = phase4["ai_provider"]["attempts"][0]
        assert skipped["outcome"] == "skipped" and "unusable this run" in skipped["reason"]

    def test_rejected_credentials_are_dead_for_the_rest_of_the_run(self):
        gemini = ScriptedProvider([ProviderUnavailableError("gemini", "credentials rejected (403)")])
        nvidia = ScriptedProvider([PHASE2_OK, PHASE4_OK], name="nvidia")
        r = router(["gemini", "nvidia"], {"gemini": gemini, "nvidia": nvidia})
        r.analyze_intelligence_report({})
        r.analyze_ticker_details({"EWJ": {}})
        assert gemini.calls == 1

    def test_circuit_opens_after_two_failures_and_stops_calling_the_provider(self):
        gemini = ScriptedProvider([rate_limited(), rate_limited()])
        nvidia = ScriptedProvider([PHASE2_OK, PHASE2_OK, PHASE2_OK], name="nvidia")
        r = router(["gemini", "nvidia"], {"gemini": gemini, "nvidia": nvidia})
        r.analyze_intelligence_report({})
        r.analyze_intelligence_report({})
        third = r.analyze_intelligence_report({})  # circuit open: gemini not even attempted
        assert gemini.calls == 2
        assert third["ai_provider"]["attempts"][0]["reason"].startswith("circuit open")

    def test_provider_whose_window_cannot_hold_the_prompt_is_skipped_without_a_breaker_penalty(self):
        pc = composer()
        pc.compose_phase2_prompt.side_effect = (
            lambda data, token_ceiling=None: (_ for _ in ()).throw(PromptBudgetError("window too small"))
            if token_ceiling and token_ceiling < 20_000 else "P2"
        )
        groq = ScriptedProvider([PHASE2_OK], name="groq")
        nvidia = ScriptedProvider([PHASE2_OK], name="nvidia")
        r = AnalystRouter(order=["groq", "nvidia"], providers={"groq": groq, "nvidia": nvidia}, prompt_composer=pc)
        result = r.analyze_intelligence_report({})
        assert groq.calls == 0  # never called: it could not even receive the prompt
        assert result["ai_provider"]["provider"] == "nvidia"
        assert result["ai_provider"]["attempts"][0]["reason"] == "window too small"
        assert r._breaker("groq").allow_request()  # not penalised: it did nothing wrong


class TestAllProvidersFail:
    def test_error_lists_every_providers_reason(self):
        providers = {
            "gemini": ScriptedProvider([rate_limited()]),
            "nvidia": ScriptedProvider([ModelNotFoundError("nvidia", "model 'm' not found")], name="nvidia"),
        }
        r = router(["gemini", "nvidia", "groq"], providers)  # groq: not configured
        with pytest.raises(AllProvidersFailedError) as exc_info:
            r.analyze_intelligence_report({})
        message = str(exc_info.value)
        assert "Phase 2" in message
        assert "gemini failed" in message and "rate limited" in message
        assert "nvidia failed" in message and "not found" in message
        assert "groq skipped" in message and "GROQ_API_KEY" in message
        assert isinstance(exc_info.value, ModelError)  # the pipeline already treats this as a failed run
        assert len(exc_info.value.attempts) == 3

    def test_never_fabricates_a_result(self):
        r = router(["gemini"], {"gemini": ScriptedProvider([rate_limited()])})
        with pytest.raises(AllProvidersFailedError):
            r.analyze_ticker_details({"EWJ": {}})


class TestDegradedProviders:
    def test_small_window_provider_marks_the_result_degraded(self):
        groq = ScriptedProvider([PHASE4_OK], name="groq")  # registry window: 6K < FULL_CONTEXT_MIN_TOKENS
        result = router(["groq"], {"groq": groq}).analyze_ticker_details({"EWJ": {}})
        assert result["ai_provider"]["degraded"] is True

    def test_full_window_provider_is_not_degraded(self):
        nvidia = ScriptedProvider([PHASE4_OK], name="nvidia")
        result = router(["nvidia"], {"nvidia": nvidia}).analyze_ticker_details({"EWJ": {}})
        assert result["ai_provider"]["degraded"] is False


class TestAttribution:
    def test_model_name_before_any_call_is_the_first_configured_provider(self):
        r = router(["gemini", "nvidia"], {"gemini": ScriptedProvider([]), "nvidia": ScriptedProvider([], name="nvidia")})
        assert r.model_name.startswith("gemini:")

    def test_model_name_after_failover_is_the_provider_that_answered(self):
        providers = {
            "gemini": ScriptedProvider([rate_limited()]),
            "nvidia": ScriptedProvider([PHASE2_OK], name="nvidia", model="nvidia/some-model"),
        }
        r = router(["gemini", "nvidia"], providers)
        r.analyze_intelligence_report({})
        assert r.model_name == "nvidia:nvidia/some-model"

    def test_orchestrator_attribution_uses_the_routers_label(self):
        pytest.importorskip("yfinance")
        pytest.importorskip("vaderSentiment")
        from modules.execution.orchestrator import ObliskOrchestrator

        providers = {"gemini": ScriptedProvider([rate_limited()]), "nvidia": ScriptedProvider([PHASE2_OK], name="nvidia")}
        r = router(["gemini", "nvidia"], providers)
        r.analyze_intelligence_report({})
        stub = ObliskOrchestrator.__new__(ObliskOrchestrator)
        stub.analyst = r
        assert stub._resolve_analyst_model() == r.model_name
        assert stub._resolve_analyst_model().startswith("nvidia:")


class TestConstruction:
    def test_no_configured_provider_fails_fast_naming_every_env_var(self):
        with pytest.raises(ValueError) as exc_info:
            AnalystRouter(order=["gemini", "openrouter"], prompt_composer=composer())
        message = str(exc_info.value)
        assert "GEMINI_API_KEY" in message and "OPENROUTER_API_KEY" in message

    def test_order_comes_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("AI_PROVIDER_ORDER", "nvidia, gemini")
        monkeypatch.setenv("GEMINI_API_KEY", "k")
        r = AnalystRouter(prompt_composer=composer())
        assert r.order == ["nvidia", "gemini"]

    def test_bad_order_in_the_environment_fails_loudly(self, monkeypatch):
        from modules.exceptions import ConfigurationError

        monkeypatch.setenv("AI_PROVIDER_ORDER", "gemini,typo")
        with pytest.raises(ConfigurationError):
            AnalystRouter(prompt_composer=composer())


class TestVerifyAll:
    def test_reports_each_provider_in_chain_order_without_calling_unconfigured_ones(self):
        providers = {"nvidia": ScriptedProvider([], name="nvidia")}
        rows = router(["gemini", "nvidia"], providers).verify_all()
        assert [r["provider"] for r in rows] == ["gemini", "nvidia"]
        assert rows[0]["configured"] is False and "GEMINI_API_KEY" in rows[0]["detail"]
        assert rows[1]["configured"] is True and rows[1]["ok"] is True
        assert rows[1]["full_context"] is True
