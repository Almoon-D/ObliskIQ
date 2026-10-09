"""
AI model providers — interchangeable TRANSPORTS for the analyst.

A provider turns a prompt string into response text. Everything that is the same
for every model (prompt composition, JSON extraction, contract validation, the
retry-with-error protocol) lives in ``llm_analyst.LLMAnalyst``; everything that
is provider-specific (SDK, HTTP shape, error vocabulary) lives here. The router
(``analyst_router``) chains providers so a failure on one falls through to the
next, in the order the operator configured.

Error taxonomy — what the router needs to decide "retry here" vs "next provider":

* ``ProviderUnavailableError`` — not configured, or credentials rejected. Never
  retried; the router skips this provider for the rest of the run.
* ``ModelNotFoundError`` — the configured model ID does not exist (free-model IDs
  rotate). Never retried; the message says which ID, so the fix is one line.
* ``ProviderCallError`` — transport / rate-limit / server error. ``retryable``
  tells ``generate`` whether to back off and try the same provider again first.
* ``ProviderResponseError`` — the call succeeded but yielded no usable text
  (empty, safety-blocked, truncated to nothing).
"""

from __future__ import annotations

import logging
import random
import time
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

from modules.config.ai_providers import ProviderSpec

logger = logging.getLogger(__name__)

# One quick retry inside a provider (rate limits and 5xx are usually transient);
# anything persistent is the router's job (next provider).
MAX_ATTEMPTS = 2
BACKOFF_BASE_S = 2.0
BACKOFF_MAX_S = 30.0
PROBE_PROMPT = "Reply with the single word OK."


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ProviderError(Exception):
    """Base class for provider failures (carries which provider, and whether to retry)."""

    def __init__(
        self,
        provider: str,
        message: str,
        *,
        retryable: bool = False,
        status: Optional[int] = None,
        retry_after_s: Optional[float] = None,
    ) -> None:
        super().__init__(f"[{provider}] {message}")
        self.provider = provider
        self.reason = message
        self.retryable = retryable
        self.status = status
        self.retry_after_s = retry_after_s


class ProviderUnavailableError(ProviderError):
    """Missing key, rejected credentials, or exhausted account: skip for this run."""


class ModelNotFoundError(ProviderError):
    """The configured model ID does not exist on this provider."""


class ProviderCallError(ProviderError):
    """HTTP / transport / rate-limit failure."""


class ProviderResponseError(ProviderError):
    """The call returned, but without usable text (empty / blocked / truncated)."""


# ---------------------------------------------------------------------------
# Base provider
# ---------------------------------------------------------------------------


class LLMProvider(ABC):
    """One model endpoint. Subclasses implement a single attempt and a health check."""

    def __init__(self, spec: ProviderSpec, *, sleep: Callable[[float], None] = time.sleep) -> None:
        self.spec = spec
        self._sleep = sleep
        self.last_usage: Dict[str, Any] = {}

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def model_name(self) -> str:
        return self.spec.model()

    @property
    def label(self) -> str:
        """``provider:model`` — the string stamped on every scoreboard row."""
        return f"{self.name}:{self.model_name}"

    @abstractmethod
    def _generate_once(self, prompt: str, max_output_tokens: int) -> str:
        """One attempt. Raise a ProviderError subclass on any failure."""

    @abstractmethod
    def verify(self, *, probe: bool = False) -> Tuple[bool, str]:
        """Cheap health check: is the configured model reachable (and, with
        ``probe``, will it actually answer)? Never raises; returns (ok, detail)."""

    def generate(self, prompt: str, *, max_output_tokens: Optional[int] = None) -> str:
        """Generate with a bounded retry on transient failures."""
        limit = max_output_tokens or self.spec.max_output_tokens
        last_error: Optional[ProviderError] = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            started = time.monotonic()
            try:
                text = self._generate_once(prompt, limit)
                logger.info(
                    "AI call ok: %s in %.1fs (~%d prompt chars) usage=%s",
                    self.label,
                    time.monotonic() - started,
                    len(prompt),
                    self.last_usage or "n/a",
                )
                return text
            except ProviderError as exc:
                last_error = exc
                if not exc.retryable or attempt == MAX_ATTEMPTS:
                    break
                delay = exc.retry_after_s
                if delay is None:
                    delay = BACKOFF_BASE_S * (2 ** (attempt - 1)) + random.uniform(0, 1)
                delay = min(delay, BACKOFF_MAX_S)
                logger.warning(
                    "AI call failed (%s): %s — retrying in %.1fs (attempt %d/%d)",
                    self.label,
                    exc.reason,
                    delay,
                    attempt,
                    MAX_ATTEMPTS,
                )
                self._sleep(delay)
        assert last_error is not None
        raise last_error

    def _require_key(self) -> str:
        key = self.spec.api_key()
        if not key:
            raise ProviderUnavailableError(
                self.name, f"{self.spec.api_key_env} is not set — provider not configured"
            )
        return key


# ---------------------------------------------------------------------------
# Gemini (google-genai SDK)
# ---------------------------------------------------------------------------


class GeminiProvider(LLMProvider):
    """Gemini through the current ``google-genai`` SDK.

    The legacy ``google-generativeai`` package reached end-of-life on 2025-11-30.
    Per Google's Gemini 3 guidance the temperature is left at its default (1.0) —
    lowering it can cause looping on reasoning tasks — and determinism comes from
    the contract validation, not from sampling.
    """

    def __init__(
        self,
        spec: ProviderSpec,
        *,
        client: Any = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        super().__init__(spec, sleep=sleep)
        self._client = client
        self._thinking_rejected = False

    def _get_client(self) -> Any:
        if self._client is None:
            key = self._require_key()
            from google import genai  # lazy: keeps import cost/failure off the offline paths

            self._client = genai.Client(api_key=key)
        return self._client

    def _build_config(self, max_output_tokens: int) -> Any:
        from google.genai import types

        kwargs: Dict[str, Any] = {
            "response_mime_type": "application/json",
            "max_output_tokens": max_output_tokens,
            "http_options": types.HttpOptions(timeout=int(self.spec.request_timeout_s * 1000)),
        }
        if self.spec.temperature is not None:
            kwargs["temperature"] = self.spec.temperature
        if self.spec.thinking_level and not self._thinking_rejected:
            kwargs["thinking_config"] = types.ThinkingConfig(
                thinking_level=types.ThinkingLevel(self.spec.thinking_level.upper())
            )
        return types.GenerateContentConfig(**kwargs)

    def _map_api_error(self, exc: Exception) -> ProviderError:
        """Translate an SDK exception into the provider taxonomy."""
        from google.genai import errors

        message = str(getattr(exc, "message", None) or exc)
        if isinstance(exc, errors.ClientError):
            code = int(getattr(exc, "code", 0) or 0)
            lowered = message.lower()
            if code == 404:
                return ModelNotFoundError(
                    self.name,
                    f"model '{self.model_name}' not found (check GEMINI_MODEL / panel "
                    f"[ai] gemini_model against Google's current model list)",
                    status=404,
                )
            if code in (401, 403) or (code == 400 and "api key" in lowered):
                return ProviderUnavailableError(
                    self.name, f"credentials rejected ({code}): {message[:200]}", status=code
                )
            if code == 429:
                return ProviderCallError(
                    self.name, f"rate limited / quota exhausted (429): {message[:200]}",
                    retryable=True, status=429,
                )
            return ProviderCallError(self.name, f"client error {code}: {message[:300]}", status=code)
        if isinstance(exc, errors.ServerError):
            code = int(getattr(exc, "code", 0) or 0)
            return ProviderCallError(
                self.name, f"server error {code}: {message[:200]}", retryable=True, status=code
            )
        return ProviderCallError(
            self.name, f"transport error ({type(exc).__name__}): {message[:200]}", retryable=True
        )

    def _generate_once(self, prompt: str, max_output_tokens: int) -> str:
        client = self._get_client()
        from google.genai import errors

        for _ in range(2):  # second pass only after dropping a rejected thinking config
            try:
                response = client.models.generate_content(
                    model=self.model_name,
                    contents=prompt,
                    config=self._build_config(max_output_tokens),
                )
                break
            except errors.ClientError as exc:
                if (
                    int(getattr(exc, "code", 0) or 0) == 400
                    and "thinking" in str(exc).lower()
                    and self.spec.thinking_level
                    and not self._thinking_rejected
                ):
                    logger.warning(
                        "%s rejected thinking_config; retrying once without it", self.label
                    )
                    self._thinking_rejected = True
                    continue
                raise self._map_api_error(exc) from exc
            except Exception as exc:  # SDK / transport: classify, never leak a raw SDK error
                raise self._map_api_error(exc) from exc
        else:  # pragma: no cover - loop always breaks or raises
            raise ProviderCallError(self.name, "generate_content did not complete")

        text = getattr(response, "text", None)
        usage = getattr(response, "usage_metadata", None)
        if usage is not None:
            self.last_usage = {
                "prompt": getattr(usage, "prompt_token_count", None),
                "output": getattr(usage, "candidates_token_count", None),
                "thoughts": getattr(usage, "thoughts_token_count", None),
            }
        if not text or not text.strip():
            feedback = getattr(response, "prompt_feedback", None)
            block = getattr(feedback, "block_reason", None) if feedback else None
            candidates = getattr(response, "candidates", None) or []
            finish = getattr(candidates[0], "finish_reason", None) if candidates else None
            raise ProviderResponseError(
                self.name,
                f"empty response (finish_reason={finish}, block_reason={block}) — if this is "
                f"MAX_TOKENS, thinking consumed the output budget",
                retryable=True,
            )
        return text

    def verify(self, *, probe: bool = False) -> Tuple[bool, str]:
        try:
            client = self._get_client()
            client.models.get(model=self.model_name)
        except ProviderError as exc:
            return False, exc.reason
        except Exception as exc:
            mapped = self._map_api_error(exc)
            return False, mapped.reason
        if not probe:
            return True, f"model '{self.model_name}' exists"
        try:
            self._generate_once(PROBE_PROMPT, 256)
        except ProviderError as exc:
            return False, exc.reason
        return True, f"model '{self.model_name}' answered a probe"


# ---------------------------------------------------------------------------
# OpenAI-compatible chat/completions (OpenRouter, NVIDIA NIM, Groq)
# ---------------------------------------------------------------------------

_CONTEXT_HINTS = ("context length", "context_length", "maximum context", "too long", "too many tokens")
_JSON_MODE_HINTS = ("response_format", "json_object", "json mode", "json_schema")


class OpenAICompatProvider(LLMProvider):
    """Any ``/chat/completions`` endpoint that speaks the OpenAI wire format."""

    def __init__(
        self,
        spec: ProviderSpec,
        *,
        session: Any = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        super().__init__(spec, sleep=sleep)
        self._http = session or requests
        self._json_mode_rejected = False

    def _headers(self) -> Dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._require_key()}",
            "Content-Type": "application/json",
        }
        if self.name == "openrouter":
            headers["X-Title"] = "ObliskIQ"  # optional attribution; harmless
        return headers

    @staticmethod
    def _retry_after(response: Any) -> Optional[float]:
        try:
            return float(response.headers.get("Retry-After"))
        except (TypeError, ValueError, AttributeError):
            return None

    def _classify_http_error(self, response: Any) -> ProviderError:
        status = int(response.status_code)
        try:
            body = response.text or ""
        except Exception:
            body = ""
        lowered = body.lower()
        snippet = body[:300].replace("\n", " ")
        # 410 Gone: NVIDIA retires free model IDs on a fixed date ("reached its end of life on ...").
        if status in (404, 410) or "model_not_found" in lowered or "no endpoints found" in lowered:
            return ModelNotFoundError(
                self.name,
                f"model '{self.model_name}' not found or retired ({status}) — set {self.spec.model_env} "
                f"(or the panel's [ai] {self.name}_model) to a current ID: {snippet}",
                status=status,
            )
        if status in (401, 403):
            return ProviderUnavailableError(
                self.name, f"credentials rejected ({status}): {snippet}", status=status
            )
        if status == 402:
            return ProviderUnavailableError(
                self.name, f"payment required / credits exhausted (402): {snippet}", status=status
            )
        if status == 413 or (status == 400 and any(h in lowered for h in _CONTEXT_HINTS)):
            return ProviderCallError(
                self.name, f"prompt exceeds the provider's window ({status}): {snippet}", status=status
            )
        if status == 429:
            return ProviderCallError(
                self.name, f"rate limited / quota exhausted (429): {snippet}",
                retryable=True, status=429, retry_after_s=self._retry_after(response),
            )
        if status >= 500:
            return ProviderCallError(
                self.name, f"server error {status}: {snippet}", retryable=True, status=status
            )
        return ProviderCallError(self.name, f"HTTP {status}: {snippet}", status=status)

    def _post(self, body: Dict[str, Any]) -> Any:
        url = f"{self.spec.base_url}/chat/completions"
        try:
            return self._http.post(
                url, json=body, headers=self._headers(), timeout=(15, self.spec.request_timeout_s)
            )
        except requests.Timeout as exc:
            raise ProviderCallError(self.name, f"timeout after {self.spec.request_timeout_s:.0f}s", retryable=True) from exc
        except requests.RequestException as exc:
            raise ProviderCallError(
                self.name, f"connection error ({type(exc).__name__}): {str(exc)[:200]}", retryable=True
            ) from exc

    def _generate_once(self, prompt: str, max_output_tokens: int) -> str:
        body: Dict[str, Any] = {
            "model": self.model_name,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_output_tokens,
        }
        if self.spec.temperature is not None:
            body["temperature"] = self.spec.temperature

        response: Any = None
        for _ in range(2):  # second pass only after dropping a rejected response_format
            if self.spec.json_mode and not self._json_mode_rejected:
                body["response_format"] = {"type": "json_object"}
            else:
                body.pop("response_format", None)
            response = self._post(body)
            if response.status_code == 200:
                break
            lowered = (response.text or "").lower()
            if (
                response.status_code in (400, 422)
                and "response_format" in body
                and any(h in lowered for h in _JSON_MODE_HINTS)
            ):
                logger.warning("%s rejected response_format; retrying once without it", self.label)
                self._json_mode_rejected = True
                continue
            raise self._classify_http_error(response)
        else:
            raise self._classify_http_error(response)

        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderResponseError(self.name, "response body is not JSON", retryable=True) from exc

        # OpenRouter (and some gateways) report upstream failures inside a 200.
        if isinstance(data, dict) and data.get("error") and not data.get("choices"):
            error = data["error"]
            code = int(error.get("code", 0) or 0) if isinstance(error, dict) else 0
            message = str(error.get("message", error) if isinstance(error, dict) else error)[:300]
            raise ProviderCallError(
                self.name,
                f"upstream error {code or ''}: {message}".strip(),
                retryable=code == 429 or code >= 500 or code == 0,
                status=code or None,
            )

        usage = data.get("usage") if isinstance(data, dict) else None
        if isinstance(usage, dict):
            self.last_usage = {
                "prompt": usage.get("prompt_tokens"),
                "output": usage.get("completion_tokens"),
            }
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderResponseError(self.name, "response has no choices[0].message.content", retryable=True) from exc
        if not content or not str(content).strip():
            finish = None
            try:
                finish = data["choices"][0].get("finish_reason")
            except Exception:
                pass
            raise ProviderResponseError(
                self.name,
                f"empty content (finish_reason={finish}) — reasoning may have consumed max_tokens",
                retryable=True,
            )
        return str(content)

    def verify(self, *, probe: bool = False) -> Tuple[bool, str]:
        try:
            headers = self._headers()
        except ProviderError as exc:
            return False, exc.reason

        listed: Optional[bool] = None
        try:
            response = self._http.get(f"{self.spec.base_url}/models", headers=headers, timeout=20)
            if response.status_code == 200:
                payload = response.json()
                ids: List[str] = [m.get("id", "") for m in payload.get("data", []) if isinstance(m, dict)]
                if ids:
                    listed = self.model_name in ids
            elif response.status_code in (401, 403):
                return False, f"credentials rejected ({response.status_code})"
        except Exception as exc:  # listing is advisory; a probe is the real test
            logger.debug("%s model listing unavailable: %s", self.name, exc)

        if listed is False:
            return False, f"model '{self.model_name}' is not in {self.name}'s current model list"
        if not probe:
            return True, (
                f"model '{self.model_name}' is listed" if listed else "reachable (model list unavailable)"
            )
        try:
            self._generate_once(PROBE_PROMPT, 64)
        except ProviderError as exc:
            return False, exc.reason
        return True, f"model '{self.model_name}' answered a probe"


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_provider(spec: ProviderSpec, **kwargs: Any) -> LLMProvider:
    """Construct the transport for ``spec`` (extra kwargs: test injection points)."""
    if spec.kind == "gemini":
        return GeminiProvider(spec, **kwargs)
    if spec.kind == "openai_compat":
        return OpenAICompatProvider(spec, **kwargs)
    raise ValueError(f"unknown provider kind '{spec.kind}' for provider '{spec.name}'")
