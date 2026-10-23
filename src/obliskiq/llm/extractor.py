"""The minimal EventExtractor (MASTER_PLAN §5.10; DECISIONS D70): news items in, one validated
:class:`~.events.ExtractedEvent` each, every call stored.

A run takes the items still owed to the configured model and prompt (oldest first, at most
``max_items_per_run``) and asks the model for each. The answer must be JSON that validates as an
``ExtractedEvent`` citing the item's own source id; an invalid answer is asked again once with
the validation error, and a second invalid answer fails the item closed (``invalid``, no output).
A call that fails (``failed``) leaves the item queued, up to ``max_call_failures`` calls; an
item too long for the model's context window fails closed without a call. Every call is stored
with its item, the time it was produced, the provider, the model id, the prompt version and
hash, its tokens and its cost, so H7's evidence is the series as produced.

Without the provider's key (the variable config names) the run is skipped and the items wait. A
call whose worst-case cost would take the month's spend past ``budget.monthly_usd`` is not made:
the run stops there and the rest wait for the next month. Nothing here sizes, orders or gates
anything, and nothing consumes the output yet.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import dataclasses
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, time
from typing import Literal

import httpx
from pydantic import ValidationError

from ..config.schema import ObliskConfig
from ..domain.sources import EventExtraction, ExtractionStatus, NewsItem
from ..errors import ConfigError
from ..logging_setup import get_logger
from ..store import sources as source_repo
from ..store.worker import StoreWorker
from ..util.http import RetryPolicy
from .events import ExtractedEvent
from .prompts import SYSTEM, item_prompt, output_schema, prompt_hash, source_id
from .transport import TRANSPORTS, Reply

log = get_logger("llm.extractor")

#: Prices are per million tokens.
TOKENS_PER_PRICE_UNIT = 1_000_000
#: How many validation problems a retry message and a stored error name.
SHOWN_PROBLEMS = 5

type RunState = Literal["ok", "skipped", "budget_spent"]


@dataclass(frozen=True, slots=True)
class ExtractorRun:
    """What one run did: its state, the calls stored by status, what it cost and why it
    stopped."""

    state: RunState
    reason: str
    ok: int = 0
    invalid: int = 0
    failed: int = 0
    cost_usd: float = 0.0


def _problems(error: ValidationError | ValueError) -> str:
    if isinstance(error, ValidationError):
        return "; ".join(
            f"{'.'.join(str(p) for p in item['loc']) or 'answer'}: {item['msg']}"
            for item in error.errors(include_input=False, include_url=False)[:SHOWN_PROBLEMS]
        )
    return str(error)


def month_start(now: datetime) -> datetime:
    """The first instant of ``now``'s calendar month in UTC, from which the budget counts."""
    return datetime.combine(now.astimezone(UTC).date().replace(day=1), time.min, tzinfo=UTC)


def parse_answer(content: str, item: NewsItem) -> ExtractedEvent:
    """The answer as an event; it must cite the item's own source id and nothing else."""
    event = ExtractedEvent.model_validate_json(content)
    expected = source_id(item)
    strangers = [s for s in event.sources if s != expected]
    if strangers:
        raise ValueError(f"sources must be [{expected!r}], not {event.sources!r}")
    return event


class EventExtractor:
    """Extracts the events of the queued news items."""

    def __init__(
        self,
        settings: ObliskConfig,
        *,
        client: httpx.AsyncClient,
        worker: StoreWorker,
        key: str | None,
        now: Callable[[], datetime],
    ) -> None:
        llm = settings.llm
        self._config = llm.extractor
        self._budget = llm.budget
        self._model = llm.extractor_model()
        self._provider_name = self._model.provider
        self._provider = llm.extractor_provider()
        self._chat = TRANSPORTS[self._provider.protocol]
        self._client = client
        self._worker = worker
        self._key = key
        self._now = now
        self._policy = dataclasses.replace(
            RetryPolicy.from_settings(settings.http),
            timeout_seconds=float(self._config.request_timeout_seconds),
        )
        self.prompt_hash = prompt_hash(self._config)

    async def pending(self) -> tuple[NewsItem, ...]:
        return await self._worker.run(
            source_repo.pending_news_items,
            model_id=self._model.model_id,
            prompt_hash=self.prompt_hash,
            limit=self._config.max_items_per_run,
            max_failures=self._config.max_call_failures,
        )

    async def run(self) -> ExtractorRun:
        items = await self.pending()
        if self._key is None:
            return ExtractorRun(
                "skipped",
                f"{self._provider.key_env} is unset: no call made, {len(items)} items wait",
            )
        spent = await self._worker.run(source_repo.extraction_spend, month_start(self._now()))
        counts = dict.fromkeys(ExtractionStatus, 0)
        cost = 0.0
        for item in items:
            worst = self._worst_cost(item)
            if spent + worst > self._budget.monthly_usd:
                return ExtractorRun(
                    "budget_spent",
                    f"the month's spend {spent:.4f} plus a call's worst case {worst:.4f} passes "
                    f"{self._budget.monthly_usd:.2f}; {len(items) - sum(counts.values())} items "
                    "wait",
                    counts[ExtractionStatus.OK],
                    counts[ExtractionStatus.INVALID],
                    counts[ExtractionStatus.FAILED],
                    cost,
                )
            extraction = await self.extract(item)
            await self._worker.run(source_repo.record_extraction, extraction)
            counts[extraction.status] += 1
            spent += extraction.cost_usd
            cost += extraction.cost_usd
        return ExtractorRun(
            "ok",
            f"{len(items)} items read",
            counts[ExtractionStatus.OK],
            counts[ExtractionStatus.INVALID],
            counts[ExtractionStatus.FAILED],
            cost,
        )

    def _tokens(self, characters: int) -> int:
        """An estimate of the tokens in ``characters``, rounded up."""
        return int(characters / self._budget.chars_per_token_estimate) + 1

    def _input_tokens(self, item: NewsItem) -> int:
        return self._tokens(len(SYSTEM) + len(item_prompt(item, self._config)))

    def _cost(self, input_tokens: int, output_tokens: int) -> float:
        model = self._model
        return (
            input_tokens * model.input_usd_per_million
            + output_tokens * model.output_usd_per_million
        ) / TOKENS_PER_PRICE_UNIT

    def _worst_cost(self, item: NewsItem) -> float:
        """Every attempt at its estimated input and its full output allowance; a retry also
        carries the first answer back."""
        attempts = self._config.retries_on_invalid + 1
        per_call = self._input_tokens(item) + self._config.max_output_tokens
        return attempts * self._cost(per_call, self._config.max_output_tokens)

    async def extract(self, item: NewsItem) -> EventExtraction:
        """One item's extraction: ok, invalid after the retry, or failed (the call itself)."""
        if self._key is None:
            raise ConfigError(f"{self._provider.key_env} is unset: no call can be made")
        key = self._key
        if self._input_tokens(item) + self._config.max_output_tokens > (
            self._model.context_window_tokens
        ):
            return self._record(
                item, ExtractionStatus.INVALID, 0, None,
                "the item and the answer would not fit the model's context window", 0, 0,
            )  # fmt: skip
        messages = [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": item_prompt(item, self._config)},
        ]
        used_in = used_out = attempts = 0
        problem = ""
        for _ in range(self._config.retries_on_invalid + 1):
            attempts += 1
            try:
                reply = await self._call(messages, key)
            except Exception as exc:  # the call failed: the item stays queued
                return self._record(
                    item, ExtractionStatus.FAILED, attempts, None, f"{type(exc).__name__}: {exc}",
                    used_in, used_out,
                )  # fmt: skip
            used_in += reply.input_tokens
            used_out += reply.output_tokens
            try:
                event = parse_answer(reply.content, item)
            except (ValidationError, ValueError) as exc:
                problem = _problems(exc)
                messages += [
                    {"role": "assistant", "content": reply.content},
                    {
                        "role": "user",
                        "content": f"That answer is not valid: {problem}. Answer again with one "
                        "JSON object that matches the schema, and nothing else.",
                    },
                ]
                continue
            return self._record(
                item, ExtractionStatus.OK, attempts, event.model_dump_json(), None, used_in,
                used_out,
            )  # fmt: skip
        log.warning("an answer stayed invalid; the item fails closed", extra={"problem": problem})
        return self._record(
            item, ExtractionStatus.INVALID, attempts, None, problem, used_in, used_out
        )

    async def _call(self, messages: list[dict[str, str]], key: str) -> Reply:
        return await self._chat(
            self._client,
            base_url=str(self._provider.base_url),
            key=key,
            model_id=self._model.model_id,
            messages=messages,
            schema=output_schema(),
            temperature=self._config.temperature,
            max_tokens=self._config.max_output_tokens,
            policy=self._policy,
            worst_input_tokens=self._tokens(sum(len(m["content"]) for m in messages)),
        )

    def _record(
        self,
        item: NewsItem,
        status: ExtractionStatus,
        attempts: int,
        output: str | None,
        error: str | None,
        input_tokens: int,
        output_tokens: int,
    ) -> EventExtraction:
        return EventExtraction(
            item_id=item.item_id,
            produced_at=self._now(),
            provider=self._provider_name,
            model_id=self._model.model_id,
            prompt_version=self._config.prompt_version,
            prompt_hash=self.prompt_hash,
            status=status,
            attempts=attempts,
            output=output,
            error=error,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=self._cost(input_tokens, output_tokens),
        )
