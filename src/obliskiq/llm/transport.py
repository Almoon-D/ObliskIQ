"""One exchange with a model (MASTER_PLAN §5.10), asked for a JSON object that matches a schema,
over the protocol its provider speaks: the OpenAI-compatible chat completions, or Anthropic's
Messages API. Both take the same arguments and give the same :class:`Reply`.

The provider's base URL and the key come from the caller (the key from the environment variable
config names); the key travels in a header and is never logged or put in an error. A reply that is
not one is an :class:`LlmCallError`; a reply without token counts is charged the worst case the
caller states, never nothing.

Anthropic's Messages API (``docs/knowledge/llm-providers.md``): the system text is its own field,
the answer's schema goes in ``output_config.format`` without the keywords its structured outputs
refuse (lengths, bounds, patterns; the caller validates them on the answer), the effort, where the
model has one, in ``output_config.effort``, and the API version named in config takes no
temperature. The answer is the reply's text blocks (a thinking block is not the answer); a refusal
or an answer cut at its token cap is no answer.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from ..errors import UpstreamError
from ..util.http import RetryPolicy, request_with_retry

HTTP_OK = 200
#: How much of a provider's error message an error repeats.
ERROR_EXCERPT_CHARS = 200
COMPLETIONS_PATH = "/chat/completions"
MESSAGES_PATH = "/messages"
#: Schema keywords Anthropic's structured outputs do not take.
UNSUPPORTED_KEYWORDS = frozenset(
    {
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minItems",
        "maxItems",
        "pattern",
    }
)
#: Why an Anthropic reply holds no answer.
NOT_AN_ANSWER = {"refusal": "the model refused", "max_tokens": "the answer hit its token cap"}


class LlmCallError(UpstreamError):
    """The provider gave no usable reply."""


@dataclass(frozen=True, slots=True)
class Reply:
    content: str
    input_tokens: int
    output_tokens: int


def _count(usage: Mapping[str, Any], key: str, worst: int) -> int:
    value = usage.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else worst


def _failed(response: httpx.Response, key: str) -> LlmCallError:
    excerpt = response.text[:ERROR_EXCERPT_CHARS].replace(key, "[key]")
    return LlmCallError(f"the provider answered HTTP {response.status_code}: {excerpt}")


async def chat(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    key: str,
    model_id: str,
    messages: Sequence[Mapping[str, str]],
    schema: Mapping[str, Any],
    temperature: float,
    max_tokens: int,
    policy: RetryPolicy,
    worst_input_tokens: int,
    api_version: str | None = None,
    effort: str | None = None,
) -> Reply:
    """One OpenAI-compatible completion; its content and the tokens the provider counted. The
    provider takes no API version and no effort."""
    del api_version, effort
    response = await request_with_retry(
        client,
        "POST",
        f"{base_url.rstrip('/')}{COMPLETIONS_PATH}",
        policy=policy,
        headers={"Authorization": f"Bearer {key}"},
        json_body={
            "model": model_id,
            "messages": list(messages),
            "temperature": temperature,
            "max_tokens": max_tokens,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "answer", "schema": dict(schema)},
            },
        },
    )
    if response.status_code != HTTP_OK:
        raise _failed(response, key)
    try:
        body = response.json()
        content = body["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise LlmCallError("the provider's reply holds no message") from exc
    if not isinstance(content, str):
        raise LlmCallError("the provider's message is not text")
    usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
    return Reply(
        content=content,
        input_tokens=_count(usage, "prompt_tokens", worst_input_tokens),
        output_tokens=_count(usage, "completion_tokens", max_tokens),
    )


def strict_schema(node: Any) -> Any:
    """``node`` without the keywords Anthropic's structured outputs refuse."""
    if isinstance(node, dict):
        return {k: strict_schema(v) for k, v in node.items() if k not in UNSUPPORTED_KEYWORDS}
    if isinstance(node, list):
        return [strict_schema(value) for value in node]
    return node


async def messages_api(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    key: str,
    model_id: str,
    messages: Sequence[Mapping[str, str]],
    schema: Mapping[str, Any],
    temperature: float,
    max_tokens: int,
    policy: RetryPolicy,
    worst_input_tokens: int,
    api_version: str | None = None,
    effort: str | None = None,
) -> Reply:
    """One Anthropic message; its answer and the tokens the provider counted. ``temperature`` is
    not sent: the API version in force refuses it. Built from the documentation, no key was at
    hand: VERIFY-LIVE(the Messages API's first structured answer)."""
    del temperature
    if api_version is None:
        raise LlmCallError("an Anthropic call names its API version")
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    output: dict[str, Any] = {"format": {"type": "json_schema", "schema": strict_schema(schema)}}
    if effort is not None:
        output["effort"] = effort
    body: dict[str, Any] = {
        "model": model_id,
        "max_tokens": max_tokens,
        "messages": [
            {"role": m["role"], "content": m["content"]} for m in messages if m["role"] != "system"
        ],
        "output_config": output,
    }
    if system:
        body["system"] = system
    response = await request_with_retry(
        client,
        "POST",
        f"{base_url.rstrip('/')}{MESSAGES_PATH}",
        policy=policy,
        headers={"x-api-key": key, "anthropic-version": api_version},
        json_body=body,
    )
    if response.status_code != HTTP_OK:
        raise _failed(response, key)
    try:
        reply = response.json()
        blocks = reply["content"]
        texts = [b["text"] for b in blocks if isinstance(b, dict) and b.get("type") == "text"]
    except (ValueError, KeyError, TypeError) as exc:
        raise LlmCallError("the provider's reply holds no message") from exc
    stop = reply.get("stop_reason")
    if stop in NOT_AN_ANSWER:
        raise LlmCallError(f"{NOT_AN_ANSWER[stop]} (stop_reason {stop})")
    if not texts or not all(isinstance(text, str) for text in texts):
        raise LlmCallError("the provider's reply holds no text")
    usage = reply.get("usage") if isinstance(reply.get("usage"), dict) else {}
    return Reply(
        content="".join(texts),
        input_tokens=_count(usage, "input_tokens", worst_input_tokens),
        output_tokens=_count(usage, "output_tokens", max_tokens),
    )


#: The transport of each protocol a provider may speak.
TRANSPORTS = {"openai_compatible": chat, "anthropic": messages_api}
