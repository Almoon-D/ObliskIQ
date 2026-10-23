"""One chat completion over the OpenAI-compatible protocol (MASTER_PLAN §5.10), asked for a
JSON object that matches a schema.

The provider's base URL and the key come from the caller (the key from the environment variable
config names); the key travels in the ``Authorization`` header and is never logged or put in an
error. A reply that is not one is an :class:`LlmCallError`; a reply without token counts is
charged the worst case the caller states, never nothing.

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
) -> Reply:
    """One completion; its content and the tokens the provider counted."""
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
        excerpt = response.text[:ERROR_EXCERPT_CHARS].replace(key, "[key]")
        raise LlmCallError(f"the provider answered HTTP {response.status_code}: {excerpt}")
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


#: The transport of each protocol a provider may speak.
TRANSPORTS = {"openai_compatible": chat}
