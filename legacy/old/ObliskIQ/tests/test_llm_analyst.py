"""
Tests for the provider-independent analyst (LLMAnalyst), JSON extraction, and the
Gemini single-provider wrapper.

Replaces the legacy-SDK tests of GeminiAnalyst (which mocked google.generativeai):
the behaviours they covered — parsing, reflexivity clamping, markdown stripping,
the Phase 4 contract — are properties of the analyst LOGIC, so they are exercised
here against a scripted provider and hold for every provider in the failover chain.
"""

import json
from unittest.mock import MagicMock

import pytest

from llm_fakes import ScriptedProvider
from modules.analysis.llm_analyst import LLMAnalyst, clean_model_json
from modules.analysis.providers import ProviderCallError
from modules.config.settings import DEFAULT_GEMINI_MODEL
from modules.exceptions import ModelError

pytestmark = pytest.mark.unit


# ----------------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------------


@pytest.fixture
def phase2_response():
    return {
        "market_regime": "bull",
        "regime_confidence": 8,
        "macro_summary": "Strong growth with contained inflation",
        "suggested_tickers": ["AAPL", "MSFT", "GOOGL"],
        "ticker_rationale": {"AAPL": "AI integration", "MSFT": "Cloud", "GOOGL": "Search moat"},
        "sector_allocation": {"Technology": 40, "Healthcare": 15},
        "risk_level": "medium",
    }


@pytest.fixture
def phase4_response():
    return {
        "schema_version": "1.0",
        "portfolio_decisions": [
            {
                "ticker": "AAPL",
                "action": "BUY",
                "conviction": 0.8,
                "size_pct": 10.0,
                "thesis": "Strong fundamentals with an AI catalyst underpriced by consensus.",
                "invalidation_triggers": [
                    {"trigger_id": "aapl-t1", "type": "price_below", "threshold": 160.0,
                     "description": "Breaks major support"}
                ],
                "win_probability": 0.60,
                "payoff_ratio": 2.0,
                "entry_price": 180.0,
                "target_price": 200.0,
            }
        ],
        "market_regime": "bull",
    }


def make_analyst(responses, **provider_kwargs):
    provider = ScriptedProvider(responses, **provider_kwargs)
    composer = MagicMock()
    composer.compose_phase2_prompt.return_value = "PHASE2 PROMPT"
    composer.compose_phase4_prompt.return_value = "PHASE4 PROMPT"
    return LLMAnalyst(provider, use_v33_prompts=True, prompt_composer=composer), provider, composer


# ----------------------------------------------------------------------------
# JSON extraction
# ----------------------------------------------------------------------------


class TestCleanModelJson:
    def test_plain_json_unchanged(self):
        assert clean_model_json('{"a": 1}') == '{"a": 1}'

    def test_markdown_fence_removed(self):
        assert json.loads(clean_model_json('```json\n{"a": 1}\n```')) == {"a": 1}

    def test_surrounding_whitespace_removed(self):
        assert json.loads(clean_model_json('  \n\n{"a": 1}\n\n  ')) == {"a": 1}

    def test_inline_reasoning_block_removed(self):
        text = '<think>let me weigh the macro picture {not json}</think>\n{"a": 1}'
        assert json.loads(clean_model_json(text)) == {"a": 1}

    def test_thinking_tag_variant_removed(self):
        assert json.loads(clean_model_json('<thinking>step 1</thinking>{"a": 2}')) == {"a": 2}

    def test_unclosed_reasoning_prefix_removed(self):
        assert json.loads(clean_model_json('<think>endless musing...\n{"a": 3}')) == {"a": 3}

    def test_prose_around_json_is_ignored(self):
        text = 'Sure! Here is the analysis you asked for:\n{"a": {"b": 2}}\nHope that helps.'
        assert json.loads(clean_model_json(text)) == {"a": {"b": 2}}

    def test_braces_inside_strings_do_not_confuse_extraction(self):
        text = 'Result: {"note": "use } and { carefully", "n": 1} trailing'
        assert json.loads(clean_model_json(text)) == {"note": "use } and { carefully", "n": 1}

    def test_no_json_returns_stripped_text_so_the_parse_error_is_real(self):
        assert clean_model_json("  This is not valid JSON  ") == "This is not valid JSON"

    def test_empty_input(self):
        assert clean_model_json("") == ""
        assert clean_model_json(None) == ""


# ----------------------------------------------------------------------------
# Phase 2
# ----------------------------------------------------------------------------


class TestPhase2:
    def test_basic_analysis(self, phase2_response):
        analyst, provider, composer = make_analyst([json.dumps(phase2_response)])
        result = analyst.analyze_intelligence_report({"regime_analysis": {}})
        assert result["market_regime"] == "bull"
        assert result["regime_confidence"] == 8
        assert result["suggested_tickers"] == ["AAPL", "MSFT", "GOOGL"]
        assert result["sector_allocation"]["Technology"] == 40
        assert provider.calls == 1

    def test_prompt_is_sized_to_the_providers_window(self, phase2_response):
        analyst, _, composer = make_analyst([json.dumps(phase2_response)], max_input_tokens=123_456)
        analyst.analyze_intelligence_report({})
        composer.compose_phase2_prompt.assert_called_once()
        assert composer.compose_phase2_prompt.call_args.kwargs["token_ceiling"] == 123_456

    def test_markdown_wrapped_response_parses(self, phase2_response):
        analyst, _, _ = make_analyst([f"```json\n{json.dumps(phase2_response)}\n```"])
        assert analyst.analyze_intelligence_report({})["market_regime"] == "bull"

    def test_object_form_tickers_are_normalised(self):
        """The shape the OutputSchema module actually asks the AI for."""
        response = {
            "market_regime": "Reflexivity",
            "regime_confidence": 9,
            "reflexivity_analysis": {
                "intensity_score": 0.72,
                "intensity_regime": "strong",
                "component_scores": {"sentiment_extremes": 0.8},
                "consensus_belief": "Yen carry trade is safe",
                "catalyst": "BoJ meeting",
            },
            "suggested_tickers": [
                {"ticker": "EWJ", "conviction": 8, "thesis": "Governance reform re-rating"},
                {"ticker": "005930.KS", "conviction": 7, "thesis": "Memory cycle turn"},
                {"ticker": "EWJ", "conviction": 5, "thesis": "duplicate"},
            ],
        }
        analyst, _, _ = make_analyst([json.dumps(response)])
        result = analyst.analyze_intelligence_report({})
        assert result["suggested_tickers"] == ["EWJ", "005930.KS"]  # strings, order kept, de-duplicated
        assert result["market_regime"] == "reflexivity"
        assert result["reflexivity_intensity"] == pytest.approx(0.72)  # lifted from the nested object
        assert result["consensus_belief"] == "Yen carry trade is safe"
        assert result["component_scores"] == {"sentiment_extremes": 0.8}
        assert result["ticker_details"]["EWJ"]["conviction"] == 8
        assert result["ticker_rationale"]["005930.KS"] == "Memory cycle turn"

    @pytest.mark.parametrize("raw, expected", [(0.75, 0.75), (1.5, 1.0), (-0.5, 0.0), (None, 0.0)])
    def test_reflexivity_intensity_clamped_or_defaulted(self, raw, expected):
        payload = {"market_regime": "bull", "suggested_tickers": ["AAPL"]}
        if raw is not None:
            payload["reflexivity_intensity"] = raw
        analyst, _, _ = make_analyst([json.dumps(payload)])
        assert analyst.analyze_intelligence_report({})["reflexivity_intensity"] == expected

    def test_regime_confidence_is_clamped_to_scale(self):
        payload = {"market_regime": "bull", "suggested_tickers": ["AAPL"], "regime_confidence": 42}
        analyst, _, _ = make_analyst([json.dumps(payload)])
        assert analyst.analyze_intelligence_report({})["regime_confidence"] == 10.0

    def test_malformed_then_valid_retries_once_with_the_error(self, phase2_response):
        analyst, provider, _ = make_analyst(["This is not valid JSON", json.dumps(phase2_response)])
        result = analyst.analyze_intelligence_report({})
        assert provider.calls == 2
        assert "PREVIOUS RESPONSE REJECTED" in provider.prompts[1]
        assert "not valid JSON" in provider.prompts[1]
        assert result["suggested_tickers"][0] == "AAPL"

    def test_invalid_json_twice_raises_model_error(self):
        analyst, provider, _ = make_analyst(["nope", "still nope"])
        with pytest.raises(ModelError, match="proposal contract"):
            analyst.analyze_intelligence_report({})
        assert provider.calls == 2

    @pytest.mark.parametrize(
        "payload",
        [
            {"market_regime": "bear", "suggested_tickers": []},
            {"market_regime": "bear"},
            {"suggested_tickers": ["AAPL"]},
            {"market_regime": "bear", "suggested_tickers": ["AAPL", ""]},
            {"market_regime": "bear", "suggested_tickers": [{"name": "no ticker field"}]},
            {"market_regime": "bear", "suggested_tickers": [42]},
        ],
    )
    def test_contract_violations_are_rejected_after_one_retry(self, payload):
        analyst, provider, _ = make_analyst([json.dumps(payload), json.dumps(payload)])
        with pytest.raises(ModelError):
            analyst.analyze_intelligence_report({})
        assert provider.calls == 2

    def test_provider_error_propagates_untouched(self):
        error = ProviderCallError("gemini", "rate limited", retryable=False, status=429)
        analyst, _, _ = make_analyst([error])
        with pytest.raises(ProviderCallError, match="rate limited"):
            analyst.analyze_intelligence_report({})


# ----------------------------------------------------------------------------
# Phase 4
# ----------------------------------------------------------------------------


class TestPhase4:
    def test_basic_decisions_are_bridged(self, phase4_response):
        analyst, provider, composer = make_analyst([json.dumps(phase4_response)])
        result = analyst.analyze_ticker_details({"AAPL": {"price": 180}})
        decision = result["portfolio_decisions"][0]
        assert decision["action"] == "BUY"
        assert decision["conviction"] == 8.0 and decision["conviction_contract"] == 0.8
        assert decision["position_size"] == pytest.approx(0.10)
        assert provider.calls == 1

    def test_prompt_is_sized_to_the_providers_window(self, phase4_response):
        analyst, _, composer = make_analyst([json.dumps(phase4_response)], max_input_tokens=77_000)
        analyst.analyze_ticker_details({"AAPL": {}})
        assert composer.compose_phase4_prompt.call_args.kwargs["token_ceiling"] == 77_000

    def test_phase2_reflexivity_is_injected_into_the_payload(self, phase4_response):
        analyst, _, composer = make_analyst([json.dumps(phase4_response)])
        data = {"AAPL": {}}
        analyst.analyze_ticker_details(
            data, phase2_analysis={"reflexivity_intensity": 0.7, "component_scores": {"x": 1}}
        )
        injected = composer.compose_phase4_prompt.call_args.args[0]["_phase2_reflexivity"]
        assert injected["intensity"] == 0.7
        assert injected["components"] == {"x": 1}

    def test_reasoning_tags_do_not_break_contract_parsing(self, phase4_response):
        analyst, _, _ = make_analyst([f"<think>sizing...</think>\n{json.dumps(phase4_response)}"])
        assert analyst.analyze_ticker_details({"AAPL": {}})["portfolio_decisions"]


# ----------------------------------------------------------------------------
# Legacy (v3.0) prompts and identity
# ----------------------------------------------------------------------------


class TestLegacyPromptsAndIdentity:
    def test_legacy_prompt_file_not_found(self, phase2_response):
        provider = ScriptedProvider([json.dumps(phase2_response)])
        analyst = LLMAnalyst(provider, use_v33_prompts=False)
        assert analyst.prompt_composer is None
        with pytest.raises(FileNotFoundError):
            analyst.analyze_intelligence_report({}, master_prompt_path="/nonexistent/prompt.md")

    def test_legacy_requires_a_prompt_path(self, phase2_response):
        analyst = LLMAnalyst(ScriptedProvider([json.dumps(phase2_response)]), use_v33_prompts=False)
        with pytest.raises(ValueError, match="master_prompt_path required"):
            analyst.analyze_intelligence_report({})

    def test_legacy_prompt_with_a_real_file(self, tmp_path, phase2_response):
        prompt_file = tmp_path / "master.md"
        prompt_file.write_text("MASTER")
        provider = ScriptedProvider([json.dumps(phase2_response)])
        analyst = LLMAnalyst(provider, use_v33_prompts=False)
        assert analyst.analyze_intelligence_report({}, master_prompt_path=str(prompt_file))["market_regime"] == "bull"
        assert provider.prompts[0].startswith("MASTER")

    def test_identity_properties(self):
        analyst, provider, _ = make_analyst([], model="some-model")
        assert analyst.model_name == "some-model"
        assert analyst.label == "gemini:some-model"
        assert analyst.token_ceiling == provider.spec.max_input_tokens


class TestGeminiAnalystWrapper:
    def test_requires_an_api_key(self, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        from modules.analysis.gemini_integration import GeminiAnalyst

        with pytest.raises(ValueError, match="GEMINI_API_KEY"):
            GeminiAnalyst()

    def test_default_model_is_the_single_source_of_truth(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        monkeypatch.delenv("GEMINI_MODEL", raising=False)
        from modules.analysis.gemini_integration import GeminiAnalyst

        analyst = GeminiAnalyst()
        assert analyst.model_name == DEFAULT_GEMINI_MODEL
        assert analyst.label == f"gemini:{DEFAULT_GEMINI_MODEL}"
        assert analyst.use_v33_prompts is True and analyst.prompt_composer is not None

    def test_model_env_override(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        monkeypatch.setenv("GEMINI_MODEL", "gemini-custom")
        from modules.analysis.gemini_integration import GeminiAnalyst

        assert GeminiAnalyst().model_name == "gemini-custom"

    def test_default_model_is_not_the_nonexistent_pro_thinking_id(self):
        """Regression: 'gemini-3.5-pro-thinking' does not exist and Pro is paid-only."""
        assert DEFAULT_GEMINI_MODEL != "gemini-3.5-pro-thinking"
        assert "pro" not in DEFAULT_GEMINI_MODEL.lower()
