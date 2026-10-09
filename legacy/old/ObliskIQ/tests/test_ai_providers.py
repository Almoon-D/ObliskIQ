"""
Tests for the provider registry (config/ai_providers.py) and the provider
transports (analysis/providers.py): error classification, bounded retry, model-ID
failures, the response_format fallback, and the real google-genai SDK wiring.

No network: OpenAI-compatible providers get an injected fake HTTP session; the
Gemini provider gets an injected fake client (the SDK's real exception and config
types are used, so a renamed SDK field fails here, not on the first live run).
"""

import copy
import dataclasses
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import requests

from modules.analysis.providers import (
    MAX_ATTEMPTS,
    GeminiProvider,
    ModelNotFoundError,
    OpenAICompatProvider,
    ProviderCallError,
    ProviderResponseError,
    ProviderUnavailableError,
    build_provider,
)
from modules.config.ai_providers import (
    DEFAULT_PROVIDER_ORDER,
    FULL_CONTEXT_MIN_TOKENS,
    PROVIDER_SPECS,
    parse_provider_order,
)
from modules.config.settings import DEFAULT_GEMINI_MODEL
from modules.exceptions import ConfigurationError

pytestmark = pytest.mark.unit


# ----------------------------------------------------------------------------
# Registry
# ----------------------------------------------------------------------------


class TestRegistry:
    def test_default_order_covers_every_provider_exactly_once(self):
        assert sorted(DEFAULT_PROVIDER_ORDER) == sorted(PROVIDER_SPECS)
        assert DEFAULT_PROVIDER_ORDER[0] == "gemini"  # free Flash first

    def test_gemini_default_is_the_single_source_of_truth(self):
        assert PROVIDER_SPECS["gemini"].default_model == DEFAULT_GEMINI_MODEL

    def test_env_var_names_are_distinct(self):
        keys = [s.api_key_env for s in PROVIDER_SPECS.values()]
        models = [s.model_env for s in PROVIDER_SPECS.values()]
        assert len(set(keys)) == len(keys) and len(set(models)) == len(models)

    def test_specs_hold_no_secret(self):
        for spec in PROVIDER_SPECS.values():
            assert not any(len(str(v)) > 80 and "key" in str(v).lower() for v in dataclasses.asdict(spec).values())

    def test_full_context_flag(self):
        assert PROVIDER_SPECS["gemini"].full_context
        assert PROVIDER_SPECS["openrouter"].full_context
        assert PROVIDER_SPECS["nvidia"].full_context
        assert not PROVIDER_SPECS["groq"].full_context  # ~8K TPM free tier
        assert PROVIDER_SPECS["groq"].max_input_tokens < FULL_CONTEXT_MIN_TOKENS

    def test_model_env_override_and_default(self, monkeypatch):
        spec = PROVIDER_SPECS["openrouter"]
        monkeypatch.delenv("OPENROUTER_MODEL", raising=False)
        assert spec.model() == spec.default_model
        monkeypatch.setenv("OPENROUTER_MODEL", "some/other-model:free")
        assert spec.model() == "some/other-model:free"
        monkeypatch.setenv("OPENROUTER_MODEL", "   ")
        assert spec.model() == spec.default_model  # blank is ignored

    def test_api_key_comes_from_env_only(self, monkeypatch):
        spec = PROVIDER_SPECS["nvidia"]
        monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
        assert spec.api_key() == ""
        monkeypatch.setenv("NVIDIA_API_KEY", "  nvapi-xyz ")
        assert spec.api_key() == "nvapi-xyz"

    def test_gemini_temperature_is_left_at_its_default(self):
        """Google: keep Gemini 3 at temperature 1.0 (lowering can cause looping)."""
        assert PROVIDER_SPECS["gemini"].temperature is None


class TestParseProviderOrder:
    @pytest.mark.parametrize("raw", [None, "", "   "])
    def test_blank_is_the_default_order(self, raw):
        assert parse_provider_order(raw) == list(DEFAULT_PROVIDER_ORDER)

    def test_custom_order_whitespace_and_case_insensitive(self):
        assert parse_provider_order(" NVIDIA , gemini ,openrouter") == ["nvidia", "gemini", "openrouter"]

    def test_subset_disables_the_rest(self):
        assert parse_provider_order("gemini") == ["gemini"]

    def test_unknown_provider_fails_loudly(self):
        with pytest.raises(ConfigurationError, match="unknown AI provider 'gemin'"):
            parse_provider_order("gemin,groq")

    def test_duplicate_fails_loudly(self):
        with pytest.raises(ConfigurationError, match="more than once"):
            parse_provider_order("gemini,groq,gemini")

    def test_only_separators_fails_loudly(self):
        with pytest.raises(ConfigurationError, match="names no providers"):
            parse_provider_order(" , ,")


# ----------------------------------------------------------------------------
# OpenAI-compatible transport
# ----------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status=200, body=None, text=None, headers=None):
        self.status_code = status
        self._body = body
        self.text = text if text is not None else (str(body) if body is not None else "")
        self.headers = headers or {}

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


def ok(content="OK", usage=None):
    body = {"choices": [{"message": {"content": content}, "finish_reason": "stop"}]}
    if usage:
        body["usage"] = usage
    return FakeResponse(200, body)


class FakeSession:
    """Replays HTTP outcomes (FakeResponse or an exception) and records requests."""

    def __init__(self, posts=(), gets=()):
        self.posts = list(posts)
        self.gets = list(gets)
        self.post_calls = []
        self.get_calls = []

    def post(self, url, json=None, headers=None, timeout=None):
        # Snapshot the body: a real HTTP client serialises it at call time, while the
        # provider reuses (and mutates) one dict across attempts.
        self.post_calls.append({"url": url, "json": copy.deepcopy(json), "headers": headers, "timeout": timeout})
        item = self.posts.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def get(self, url, headers=None, timeout=None):
        self.get_calls.append({"url": url, "headers": headers})
        item = self.gets.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def keyed(monkeypatch):
    for var in ("OPENROUTER_API_KEY", "NVIDIA_API_KEY", "GROQ_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.setenv(var, "secret-test-key")
    for var in ("OPENROUTER_MODEL", "NVIDIA_MODEL", "GROQ_MODEL", "GEMINI_MODEL"):
        monkeypatch.delenv(var, raising=False)


def provider_for(name, session, sleeps=None):
    sleeps = sleeps if sleeps is not None else []
    return OpenAICompatProvider(PROVIDER_SPECS[name], session=session, sleep=sleeps.append)


class TestOpenAICompatRequests:
    def test_request_shape_and_success(self, keyed):
        session = FakeSession([ok('{"a": 1}', usage={"prompt_tokens": 12, "completion_tokens": 3})])
        provider = provider_for("openrouter", session)
        assert provider.generate("hello") == '{"a": 1}'
        call = session.post_calls[0]
        assert call["url"] == "https://openrouter.ai/api/v1/chat/completions"
        assert call["headers"]["Authorization"] == "Bearer secret-test-key"
        body = call["json"]
        assert body["model"] == PROVIDER_SPECS["openrouter"].default_model
        assert body["messages"] == [{"role": "user", "content": "hello"}]
        assert body["max_tokens"] == PROVIDER_SPECS["openrouter"].max_output_tokens
        assert body["temperature"] == 0.2
        assert body["response_format"] == {"type": "json_object"}
        assert call["timeout"] == (15, PROVIDER_SPECS["openrouter"].request_timeout_s)
        assert provider.last_usage == {"prompt": 12, "output": 3}

    @pytest.mark.parametrize(
        "name, url",
        [
            ("nvidia", "https://integrate.api.nvidia.com/v1/chat/completions"),
            ("groq", "https://api.groq.com/openai/v1/chat/completions"),
        ],
    )
    def test_each_provider_hits_its_own_endpoint(self, keyed, name, url):
        session = FakeSession([ok()])
        provider_for(name, session).generate("x")
        assert session.post_calls[0]["url"] == url

    def test_label_reflects_model_override(self, keyed, monkeypatch):
        monkeypatch.setenv("NVIDIA_MODEL", "meta/llama-x")
        assert provider_for("nvidia", FakeSession()).label == "nvidia:meta/llama-x"

    def test_missing_key_is_unavailable_and_never_hits_the_network(self, monkeypatch):
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        session = FakeSession()
        with pytest.raises(ProviderUnavailableError, match="GROQ_API_KEY"):
            provider_for("groq", session).generate("x")
        assert session.post_calls == []


class TestOpenAICompatErrors:
    def test_rate_limit_retries_after_the_server_hint_then_succeeds(self, keyed):
        sleeps = []
        session = FakeSession([FakeResponse(429, text="slow down", headers={"Retry-After": "7"}), ok("done")])
        assert provider_for("openrouter", session, sleeps).generate("x") == "done"
        assert sleeps == [7.0]
        assert len(session.post_calls) == 2

    def test_persistent_rate_limit_raises_after_bounded_attempts(self, keyed):
        sleeps = []
        session = FakeSession([FakeResponse(429, text="no")] * MAX_ATTEMPTS)
        with pytest.raises(ProviderCallError) as exc_info:
            provider_for("openrouter", session, sleeps).generate("x")
        assert exc_info.value.status == 429 and exc_info.value.retryable
        assert len(session.post_calls) == MAX_ATTEMPTS
        assert len(sleeps) == MAX_ATTEMPTS - 1

    def test_server_error_is_retryable(self, keyed):
        session = FakeSession([FakeResponse(503, text="overloaded"), ok("fine")])
        assert provider_for("nvidia", session).generate("x") == "fine"

    @pytest.mark.parametrize("status", [401, 403])
    def test_bad_credentials_are_unavailable_and_not_retried(self, keyed, status):
        session = FakeSession([FakeResponse(status, text="invalid api key")])
        with pytest.raises(ProviderUnavailableError, match="credentials rejected"):
            provider_for("groq", session).generate("x")
        assert len(session.post_calls) == 1

    def test_payment_required_is_unavailable(self, keyed):
        session = FakeSession([FakeResponse(402, text="insufficient credits")])
        with pytest.raises(ProviderUnavailableError, match="credits exhausted"):
            provider_for("openrouter", session).generate("x")

    def test_unknown_model_names_the_env_var_to_fix(self, keyed):
        session = FakeSession([FakeResponse(404, text="No endpoints found for model x")])
        with pytest.raises(ModelNotFoundError, match="OPENROUTER_MODEL"):
            provider_for("openrouter", session).generate("x")
        assert len(session.post_calls) == 1  # never retried

    def test_a_retired_model_is_named_as_such_with_the_setting_to_change(self, keyed):
        """NVIDIA answered HTTP 410 "reached its end of life on 2026-10-03" for the default model in Paper Run
        #188; it surfaced as a bare "HTTP 410" with no hint of what to change."""
        body = '{"title":"Gone","status":410,"detail":"The model \'x\' has reached its end of life"}'
        session = FakeSession([FakeResponse(410, text=body)])
        with pytest.raises(ModelNotFoundError, match="NVIDIA_MODEL"):
            provider_for("nvidia", session).generate("x")
        assert len(session.post_calls) == 1  # never retried

    @pytest.mark.parametrize(
        "status, text",
        [(413, "payload too large"), (400, "This model's maximum context length is 8192 tokens")],
    )
    def test_oversized_prompt_is_not_retried(self, keyed, status, text):
        session = FakeSession([FakeResponse(status, text=text)])
        with pytest.raises(ProviderCallError, match="window") as exc_info:
            provider_for("groq", session).generate("x")
        assert not exc_info.value.retryable
        assert len(session.post_calls) == 1

    def test_response_format_rejection_is_retried_once_without_it_and_remembered(self, keyed):
        session = FakeSession(
            [FakeResponse(400, text="response_format json_object is not supported by this model"), ok("A"), ok("B")]
        )
        provider = provider_for("nvidia", session)
        assert provider.generate("one") == "A"
        assert "response_format" in session.post_calls[0]["json"]
        assert "response_format" not in session.post_calls[1]["json"]
        assert provider.generate("two") == "B"
        assert "response_format" not in session.post_calls[2]["json"]  # remembered

    def test_timeout_and_connection_errors_are_retryable(self, keyed):
        session = FakeSession([requests.Timeout("slow"), requests.ConnectionError("reset"), ok("late")])
        with pytest.raises(ProviderCallError, match="connection error"):
            provider_for("openrouter", session).generate("x")  # 2 attempts: timeout, then connection error
        session2 = FakeSession([requests.Timeout("slow"), ok("late")])
        assert provider_for("openrouter", session2).generate("x") == "late"

    def test_openrouter_style_error_inside_a_200(self, keyed):
        body = {"error": {"code": 429, "message": "upstream rate limited"}}
        session = FakeSession([FakeResponse(200, body), ok("recovered")])
        assert provider_for("openrouter", session).generate("x") == "recovered"

    @pytest.mark.parametrize(
        "body",
        [
            {"choices": [{"message": {"content": ""}, "finish_reason": "length"}]},
            {"choices": [{"message": {"content": None}}]},
            {"choices": []},
            {"unexpected": True},
        ],
    )
    def test_unusable_replies_are_response_errors(self, keyed, body):
        session = FakeSession([FakeResponse(200, body)] * MAX_ATTEMPTS)
        with pytest.raises(ProviderResponseError):
            provider_for("nvidia", session).generate("x")

    def test_non_json_body_is_a_response_error(self, keyed):
        session = FakeSession([FakeResponse(200, body=None, text="<html>gateway</html>")] * MAX_ATTEMPTS)
        with pytest.raises(ProviderResponseError, match="not JSON"):
            provider_for("nvidia", session).generate("x")


class TestOpenAICompatVerify:
    def test_listed_model_is_ok(self, keyed):
        model = PROVIDER_SPECS["openrouter"].default_model
        session = FakeSession(gets=[FakeResponse(200, {"data": [{"id": model}, {"id": "other"}]})])
        assert provider_for("openrouter", session).verify() == (True, f"model '{model}' is listed")

    def test_unlisted_model_is_reported_with_its_name(self, keyed):
        session = FakeSession(gets=[FakeResponse(200, {"data": [{"id": "other"}]})])
        ok_, detail = provider_for("nvidia", session).verify()
        assert not ok_ and "not in nvidia's current model list" in detail

    def test_rejected_credentials(self, keyed):
        session = FakeSession(gets=[FakeResponse(401, text="no")])
        assert provider_for("groq", session).verify() == (False, "credentials rejected (401)")

    def test_unset_key(self, monkeypatch):
        monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
        ok_, detail = provider_for("nvidia", FakeSession()).verify()
        assert not ok_ and "NVIDIA_API_KEY" in detail

    def test_listing_failure_falls_back_to_reachable(self, keyed):
        session = FakeSession(gets=[requests.ConnectionError("dns")])
        ok_, detail = provider_for("nvidia", session).verify()
        assert ok_ and "model list unavailable" in detail

    def test_probe_makes_a_real_tiny_call(self, keyed):
        model = PROVIDER_SPECS["nvidia"].default_model
        session = FakeSession(posts=[ok("OK")], gets=[FakeResponse(200, {"data": [{"id": model}]})])
        ok_, detail = provider_for("nvidia", session).verify(probe=True)
        assert ok_ and "answered a probe" in detail
        assert session.post_calls[0]["json"]["max_tokens"] == 64

    def test_probe_failure_is_reported_not_raised(self, keyed):
        model = PROVIDER_SPECS["nvidia"].default_model
        session = FakeSession(posts=[FakeResponse(404, text="gone")], gets=[FakeResponse(200, {"data": [{"id": model}]})])
        ok_, detail = provider_for("nvidia", session).verify(probe=True)
        assert not ok_ and "not found" in detail


# ----------------------------------------------------------------------------
# Gemini transport (real google-genai types, fake client)
# ----------------------------------------------------------------------------


def gemini_response(text="{}", usage=True, finish="STOP", block=None):
    response = SimpleNamespace(
        text=text,
        usage_metadata=SimpleNamespace(prompt_token_count=100, candidates_token_count=20, thoughts_token_count=300)
        if usage
        else None,
        prompt_feedback=SimpleNamespace(block_reason=block) if block else None,
        candidates=[SimpleNamespace(finish_reason=finish)],
    )
    return response


def gemini_provider(client, sleeps=None):
    sleeps = sleeps if sleeps is not None else []
    return GeminiProvider(PROVIDER_SPECS["gemini"], client=client, sleep=sleeps.append)


def genai_error(kind, code, message="boom"):
    from google.genai import errors

    cls = errors.ClientError if kind == "client" else errors.ServerError
    return cls(code, {"error": {"code": code, "message": message, "status": "X"}}, None)


class TestGeminiProvider:
    def test_success_records_usage_and_uses_the_configured_model(self, keyed):
        client = MagicMock()
        client.models.generate_content.return_value = gemini_response('{"ok": true}')
        provider = gemini_provider(client)
        assert provider.generate("hi") == '{"ok": true}'
        kwargs = client.models.generate_content.call_args.kwargs
        assert kwargs["model"] == DEFAULT_GEMINI_MODEL
        assert kwargs["contents"] == "hi"
        assert provider.last_usage == {"prompt": 100, "output": 20, "thoughts": 300}

    def test_config_uses_json_mode_thinking_and_default_temperature(self, keyed):
        config = gemini_provider(MagicMock())._build_config(32_768)
        assert config.response_mime_type == "application/json"
        assert config.max_output_tokens == 32_768
        assert config.thinking_config.thinking_level.value == "HIGH"
        assert config.temperature is None  # Google: do not lower it for Gemini 3
        assert config.http_options.timeout == int(PROVIDER_SPECS["gemini"].request_timeout_s * 1000)

    def test_404_names_the_model_and_the_setting_to_change(self, keyed):
        client = MagicMock()
        client.models.generate_content.side_effect = genai_error("client", 404, "models/x is not found")
        with pytest.raises(ModelNotFoundError, match="GEMINI_MODEL"):
            gemini_provider(client).generate("x")
        assert client.models.generate_content.call_count == 1

    @pytest.mark.parametrize("code", [401, 403])
    def test_rejected_credentials(self, keyed, code):
        client = MagicMock()
        client.models.generate_content.side_effect = genai_error("client", code, "permission denied")
        with pytest.raises(ProviderUnavailableError):
            gemini_provider(client).generate("x")

    def test_invalid_api_key_400_is_unavailable(self, keyed):
        client = MagicMock()
        client.models.generate_content.side_effect = genai_error("client", 400, "API key not valid. Please pass a valid API key.")
        with pytest.raises(ProviderUnavailableError):
            gemini_provider(client).generate("x")

    def test_429_is_retried_then_succeeds(self, keyed):
        sleeps = []
        client = MagicMock()
        client.models.generate_content.side_effect = [genai_error("client", 429, "quota"), gemini_response("fine")]
        assert gemini_provider(client, sleeps).generate("x") == "fine"
        assert len(sleeps) == 1

    def test_persistent_server_error_raises_after_bounded_attempts(self, keyed):
        client = MagicMock()
        client.models.generate_content.side_effect = [genai_error("server", 503, "overloaded")] * MAX_ATTEMPTS
        with pytest.raises(ProviderCallError) as exc_info:
            gemini_provider(client).generate("x")
        assert exc_info.value.retryable and exc_info.value.status == 503

    def test_transport_exception_is_classified_not_leaked(self, keyed):
        client = MagicMock()
        client.models.generate_content.side_effect = [ConnectionResetError("reset")] * MAX_ATTEMPTS
        with pytest.raises(ProviderCallError, match="transport error"):
            gemini_provider(client).generate("x")

    def test_thinking_config_rejection_retries_once_without_it(self, keyed):
        client = MagicMock()
        client.models.generate_content.side_effect = [
            genai_error("client", 400, "thinking_level is not supported for this model"),
            gemini_response("ok"),
        ]
        provider = gemini_provider(client)
        assert provider.generate("x") == "ok"
        first, second = [c.kwargs["config"] for c in client.models.generate_content.call_args_list]
        assert first.thinking_config is not None and second.thinking_config is None

    def test_empty_response_explains_the_likely_cause(self, keyed):
        client = MagicMock()
        client.models.generate_content.return_value = gemini_response(text=None, finish="MAX_TOKENS")
        with pytest.raises(ProviderResponseError, match="thinking consumed the output budget"):
            gemini_provider(client).generate("x")

    def test_blocked_prompt_is_reported(self, keyed):
        client = MagicMock()
        client.models.generate_content.return_value = gemini_response(text="", block="SAFETY")
        with pytest.raises(ProviderResponseError, match="SAFETY"):
            gemini_provider(client).generate("x")

    def test_missing_key_is_unavailable_without_importing_the_sdk_client(self, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        with pytest.raises(ProviderUnavailableError, match="GEMINI_API_KEY"):
            GeminiProvider(PROVIDER_SPECS["gemini"]).generate("x")

    def test_verify_checks_the_model_exists(self, keyed):
        client = MagicMock()
        assert gemini_provider(client).verify() == (True, f"model '{DEFAULT_GEMINI_MODEL}' exists")
        client.models.get.assert_called_once_with(model=DEFAULT_GEMINI_MODEL)

    def test_verify_reports_a_missing_model(self, keyed):
        client = MagicMock()
        client.models.get.side_effect = genai_error("client", 404, "not found")
        ok_, detail = gemini_provider(client).verify()
        assert not ok_ and "not found" in detail

    def test_verify_probe(self, keyed):
        client = MagicMock()
        client.models.generate_content.return_value = gemini_response("OK")
        ok_, detail = gemini_provider(client).verify(probe=True)
        assert ok_ and "answered a probe" in detail
        assert client.models.generate_content.call_args.kwargs["config"].max_output_tokens == 256


class TestFactory:
    def test_builds_the_right_transport_per_kind(self, keyed):
        assert isinstance(build_provider(PROVIDER_SPECS["gemini"]), GeminiProvider)
        for name in ("openrouter", "nvidia", "groq"):
            assert isinstance(build_provider(PROVIDER_SPECS[name]), OpenAICompatProvider)

    def test_unknown_kind_is_rejected(self):
        spec = dataclasses.replace(PROVIDER_SPECS["groq"], kind="mystery")
        with pytest.raises(ValueError, match="unknown provider kind"):
            build_provider(spec)
