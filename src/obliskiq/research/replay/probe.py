"""The memory probe (H15): does a model recall how a name's price moved after a date?

In each calendar quarter of both windows, ``questions_per_quarter`` questions are drawn with the
registered seed, each one sheet date of the quarter and one name evaluable in that date's cohort,
the same questions for every model. A model is asked, with nothing but the ticker, the company's
name and the date, the probability that the name closed higher ``horizon_sessions`` sessions
after the first session following the date. It **hits** when it states more than one half and
the name rose, or less and it did not. A quarter is **recalled** when its hits beat the sample's
majority outcome (the share of the larger side) on a one-sided binomial test at the registered
alpha. A recalled quarter after the cutoff is trimmed from the primary statistic; the quarters
before are the control (a model that recalls none of them shows the probe has no power to see
its memory).

Each answer is stored as produced with its cost; a question already answered for a model is
never asked again. An answer that is not the schema's JSON is asked once more, then counted as
invalid and unscored.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import dataclasses
import json
import math
import random
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Final

import httpx
from pydantic import ValidationError

from ...config.schema import ObliskConfig
from ...config.schema_llm import LlmModelConfig
from ...domain.base import DomainModel
from ...domain.sources import ExtractionStatus, Probability
from ...llm.analyst_prompts import schema_of
from ...llm.extractor import TOKENS_PER_PRICE_UNIT
from ...llm.transport import TRANSPORTS
from ...store import replay as replay_repo
from ...store.replay import ProbeAnswer
from ...store.worker import StoreWorker
from ...util.http import RetryPolicy
from ..prereg_replay import AnalystReplaySpec
from .prices import Cohort

HALF: Final = 0.5
MONTHS_PER_QUARTER: Final = 3

PROBE_SYSTEM: Final = (
    "You state a probability about a past price move from what you know. Answer with one JSON "
    "object that matches the schema, and nothing else."
)


class ProbeReply(DomainModel):
    probability: Probability


def quarter_of(day: date) -> str:
    return f"{day.year}Q{(day.month - 1) // MONTHS_PER_QUARTER + 1}"


@dataclass(frozen=True, slots=True)
class Question:
    quarter: str
    seq: int
    sheet_date: date
    ticker: str
    company: str
    rose: bool


def probe_prompt(question: Question, *, sessions: int) -> str:
    return (
        f"Ticker: {question.ticker}. Company: {question.company}. Date: "
        f"{question.sheet_date.isoformat()}. What is the probability that this company's stock "
        f"closed higher {sessions} trading sessions after the first trading session following "
        "that date than it closed on that first session? Answer with "
        '{"probability": <a number from 0 to 1>}.'
    )


def draw(
    spec: AnalystReplaySpec,
    cohorts: Sequence[Cohort],
    companies: Mapping[date, Mapping[str, str]],
) -> list[Question]:
    """The probe's questions: per quarter, in date order, drawn with the registered seed among
    the evaluable names of the quarter's cohorts; ``companies`` names each ticker by date."""
    rng = random.Random(spec.probe.seed)
    by_quarter: dict[str, list[Cohort]] = {}
    for found in sorted(cohorts, key=lambda c: c.sheet):
        if found.benchmark is not None:
            by_quarter.setdefault(quarter_of(found.sheet), []).append(found)
    questions: list[Question] = []
    for quarter, members in sorted(by_quarter.items()):
        pool = sorted(
            (found.sheet, ticker, growth)
            for found in members
            for ticker, growth in found.growth.items()
        )
        chosen = rng.sample(pool, min(spec.probe.questions_per_quarter, len(pool)))
        for seq, (sheet, ticker, growth) in enumerate(sorted(chosen), start=1):
            company = companies.get(sheet, {}).get(ticker, ticker)
            questions.append(Question(quarter, seq, sheet, ticker, company, growth > 1))
    return questions


def binomial_tail(hits: int, n: int, p: float) -> float:
    """``P(X ≥ hits)`` for ``X ~ Binomial(n, p)``."""
    return sum(math.comb(n, k) * p**k * (1 - p) ** (n - k) for k in range(hits, n + 1))


@dataclass(frozen=True, slots=True)
class QuarterRecall:
    """One model's probe in one quarter."""

    model_id: str
    quarter: str
    asked: int
    answered: int
    hits: int
    majority: float
    p_value: float | None

    def recalled(self, alpha: float) -> bool:
        return self.p_value is not None and self.p_value < alpha


def score(answers: Sequence[ProbeAnswer]) -> list[QuarterRecall]:
    """Each model's hits per quarter against its sample's majority outcome."""
    groups: dict[tuple[str, str], list[ProbeAnswer]] = {}
    for answer in answers:
        groups.setdefault((answer.model_id, answer.quarter), []).append(answer)
    found = []
    for (model_id, quarter), members in sorted(groups.items()):
        ok = [a for a in members if a.probability is not None]
        hits = sum(
            1
            for a in ok
            if a.probability is not None
            and ((a.probability > HALF and a.rose) or (a.probability < HALF and not a.rose))
        )
        rose = sum(1 for a in ok if a.rose)
        majority = max(rose, len(ok) - rose) / len(ok) if ok else 0.0
        p_value = binomial_tail(hits, len(ok), majority) if ok and majority < 1 else None
        found.append(
            QuarterRecall(model_id, quarter, len(members), len(ok), hits, majority, p_value)
        )
    return found


class Probe:
    """Asks each replayed model the probe's questions not yet answered for it."""

    def __init__(
        self,
        settings: ObliskConfig,
        spec: AnalystReplaySpec,
        *,
        client: httpx.AsyncClient,
        worker: StoreWorker,
        keys: Mapping[str, str | None],
        now: Callable[[], datetime],
    ) -> None:
        self._settings = settings
        self._spec = spec
        self._client = client
        self._worker = worker
        self._keys = keys
        self._now = now
        analyst = settings.llm.analyst
        self._policy = dataclasses.replace(
            RetryPolicy.from_settings(settings.http),
            timeout_seconds=float(analyst.request_timeout_seconds),
        )
        self._schema = schema_of(ProbeReply)

    def _tokens(self, characters: int) -> int:
        return int(characters / self._settings.llm.budget.chars_per_token_estimate) + 1

    @staticmethod
    def _cost(model: LlmModelConfig, input_tokens: int, output_tokens: int) -> float:
        return (
            input_tokens * model.input_usd_per_million
            + output_tokens * model.output_usd_per_million
        ) / TOKENS_PER_PRICE_UNIT

    def worst_cost(self, name: str, questions: Sequence[Question]) -> float:
        """Every question at its full output allowance, retried once."""
        model = self._settings.llm.models[name]
        cap = self._settings.llm.replay.probe_max_output_tokens
        attempts = self._settings.llm.analyst.retries_on_invalid + 1
        sessions = self._spec.outcome.horizon_sessions
        total = 0.0
        for question in questions:
            prompt = len(PROBE_SYSTEM) + len(probe_prompt(question, sessions=sessions))
            total += attempts * self._cost(model, self._tokens(prompt) + cap, cap)
        return total

    async def pending(self, name: str, questions: Sequence[Question]) -> list[Question]:
        model = self._settings.llm.models[name]
        done = {
            (a.quarter, a.seq)
            for a in await self._worker.run(replay_repo.probes, model_id=model.model_id)
        }
        return [q for q in questions if (q.quarter, q.seq) not in done]

    async def ask(self, name: str, question: Question) -> ProbeAnswer:
        """One question to one model, stored as produced."""
        model = self._settings.llm.models[name]
        provider = self._settings.llm.provider_of(name)
        key = self._keys.get(model.provider)
        if key is None:
            raise ValueError(f"{provider.key_env} unset")
        cap = self._settings.llm.replay.probe_max_output_tokens
        messages = [
            {"role": "system", "content": PROBE_SYSTEM},
            {
                "role": "user",
                "content": probe_prompt(question, sessions=self._spec.outcome.horizon_sessions),
            },
        ]
        used_in = used_out = 0
        status, probability, problem = ExtractionStatus.INVALID, None, "no answer"
        for _ in range(self._settings.llm.analyst.retries_on_invalid + 1):
            try:
                reply = await TRANSPORTS[provider.protocol](
                    self._client,
                    base_url=str(provider.base_url),
                    key=key,
                    model_id=model.model_id,
                    messages=messages,
                    schema=self._schema,
                    temperature=self._settings.llm.analyst.temperature,
                    max_tokens=cap,
                    policy=self._policy,
                    worst_input_tokens=self._tokens(sum(len(m["content"]) for m in messages)),
                    api_version=provider.api_version,
                    effort=model.effort,
                )
            except Exception as exc:  # the call failed: nothing of it is used
                status, problem = ExtractionStatus.FAILED, f"{type(exc).__name__}: {exc}"
                break
            used_in += reply.input_tokens
            used_out += reply.output_tokens
            try:
                probability = ProbeReply.model_validate(json.loads(reply.content)).probability
            except (ValidationError, ValueError) as exc:
                problem = str(exc).splitlines()[0]
                messages = [
                    *messages,
                    {"role": "assistant", "content": reply.content},
                    {
                        "role": "user",
                        "content": f"That answer is not valid: {problem}. Answer again with one "
                        "JSON object that matches the schema, and nothing else.",
                    },
                ]
                continue
            status = ExtractionStatus.OK
            break
        answer = ProbeAnswer(
            provider=model.provider,
            model_id=model.model_id,
            quarter=question.quarter,
            seq=question.seq,
            sheet_date=question.sheet_date,
            ticker=question.ticker,
            rose=question.rose,
            status=status,
            probability=probability if status is ExtractionStatus.OK else None,
            error=None if status is ExtractionStatus.OK else problem,
            input_tokens=used_in,
            output_tokens=used_out,
            cost_usd=self._cost(model, used_in, used_out),
            produced_at=self._now(),
        )
        await self._worker.run(replay_repo.record_probe, answer)
        return answer
