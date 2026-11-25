"""The Analyst role (MASTER_PLAN §5.5, §5.10; DECISIONS D156): theses of its own from the fact
sheet, and a second opinion on each of the owner's ideas, every call stored as produced.

A run reads one fact sheet (``grid/facts.py``) and:

1. writes a **second opinion** on each of the owner's open theses that has none (``store/
   analyst.py: awaiting_opinion``): a critique and the probability it states that the target is
   reached. Logged and graded (Brier, when the thesis closes), never a veto: nothing reads it but
   the digest and ``idea show``;
2. asks for **drafts**, at most what is left of ``max_drafts_per_week`` this ISO week. Each draft
   is checked on its own and **dropped** (with the reason, logged and stored by the caller) when
   it lacks or mis-states a field, names an instrument (or a stock of the screen, as its section
   says) the sheet does not list or one an open thesis (or an earlier draft of the answer)
   already holds, cites no fact line, cites a line the sheet does not hold or one dated after the
   sheet, names an instrument without a close on the sheet or levels that do not bracket it (the
   gate judges the gain-to-loss ratio at the price it would enter at), sets a horizon outside
   ``(as_of, as_of + max_horizon_days]``, or comes beyond the week's remaining number. What
   survives is a :class:`Proposal` the service hands to the thesis book's intake as source
   ``analyst``; its gate decides. Nothing here sizes, orders or gates: ``llm`` imports nothing
   from ``decision`` or ``execution``.

Each call is temperature 0, asks for JSON matching a schema, and is asked once more with the
validation error when its answer is not that JSON (``retries_on_invalid``); then it fails closed
(``invalid``) and nothing of it is used. A call that fails outright (``failed``) uses nothing; an
opinion is asked again on later runs up to ``max_call_failures``. Without the provider's key the
run is skipped and says so. A call is made only if the month's spend on every LLM role plus its
worst case stays within ``budget.monthly_usd``; once spent there are no drafts and no opinions,
and the thesis book runs on its exits.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import dataclasses
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal

import httpx
from pydantic import ValidationError

from ..config.schema import ObliskConfig
from ..domain.analyst import UNSTORED_CALL, AnalystCall, CallKind, SecondOpinion
from ..domain.sources import ExtractionStatus
from ..domain.thesis import OPEN_STATES, Expression, Thesis, ThesisDraft, ThesisSource
from ..grid.facts import FactLine, FactSheet, number
from ..logging_setup import get_logger
from ..store import analyst as analyst_repo
from ..store import theses as thesis_repo
from ..store.worker import StoreWorker
from ..util.http import RetryPolicy
from .analyst_prompts import (
    DRAFTS_SYSTEM,
    OPINION_SYSTEM,
    STOCK_SECTION,
    DraftAnswer,
    DraftsAnswer,
    OpinionAnswer,
    analyst_prompt_hash,
    drafts_prompt,
    opinion_prompt,
    schema_of,
)
from .extractor import SHOWN_PROBLEMS, TOKENS_PER_PRICE_UNIT, month_start
from .transport import TRANSPORTS, Reply

log = get_logger("llm.analyst")

type RunState = Literal["ok", "skipped", "budget_spent"]
#: Days in a week; a week starts on Monday (ISO).
WEEK_DAYS = 7


def problems(error: ValidationError | ValueError) -> str:
    if isinstance(error, ValidationError):
        return "; ".join(
            f"{'.'.join(str(p) for p in item['loc']) or 'answer'}: {item['msg']}"
            for item in error.errors(include_input=False, include_url=False)[:SHOWN_PROBLEMS]
        )
    return str(error)


def cited_lines(refs: Sequence[str], sheet: FactSheet) -> tuple[FactLine, ...]:
    """The lines ``refs`` name, each on the sheet and dated on or before it."""
    found = []
    for ref in refs:
        line = sheet.line(ref)
        if line is None:
            raise ValueError(f"cites {ref!r}, which is not a line of the fact sheet")
        if line.day > sheet.as_of:
            raise ValueError(
                f"cites {line.ref}, dated {line.day.isoformat()}, after the sheet's date "
                f"{sheet.as_of.isoformat()}"
            )
        found.append(line)
    return tuple(found)


def facts_json(lines: Sequence[FactLine]) -> str:
    """The cited lines as they read, for the record."""
    return json.dumps([line.render() for line in lines])


@dataclass(frozen=True, slots=True)
class Proposal:
    """One draft of a drafts call: valid (``answer``) or dropped (``dropped``, why)."""

    seq: int
    raw: str
    facts: tuple[FactLine, ...]
    answer: DraftAnswer | None
    dropped: str | None

    def thesis_draft(self) -> ThesisDraft:
        """The draft as the thesis book's intake takes it, from source ``analyst``."""
        answer = self.answer
        if answer is None:
            raise ValueError(f"draft {self.seq} was dropped: {self.dropped}")
        stock = answer.section == STOCK_SECTION
        return ThesisDraft(
            source=ThesisSource.ANALYST,
            view=answer.view,
            # The sleeve's rules decide whether the idea is held as an option, never the model;
            # a listed stock has no collected option chain and is held as itself.
            expression=Expression.SHARES if stock else Expression.AUTO,
            symbol=None if stock else answer.symbol,
            listing=answer.symbol if stock else None,
            expectation=answer.expectation,
            target=answer.level("target"),
            invalidation=answer.level("invalidation"),
            horizon=answer.horizon,
        )


def check_draft(
    seq: int,
    raw: object,
    sheet: FactSheet,
    *,
    wanted: int,
    last_day: date,
    taken: Mapping[str, str] | None = None,
) -> Proposal:
    """One draft as written, checked against the sheet; ``taken`` names the instruments an open
    thesis or an earlier draft of the answer already holds (symbol to what holds it)."""
    text = json.dumps(raw, sort_keys=True, default=str)

    def drop(reason: str, facts: tuple[FactLine, ...] = ()) -> Proposal:
        log.warning("an analyst draft is dropped", extra={"seq": seq, "reason": reason})
        return Proposal(seq, text, facts, None, reason)

    if seq > wanted:
        return drop(f"beyond the {wanted} drafts left this week")
    try:
        answer = DraftAnswer.model_validate(raw)
    except ValidationError as exc:
        return drop(f"a field is missing or wrong: {problems(exc)}")
    symbol = answer.symbol.strip().upper()
    stock = answer.section == STOCK_SECTION
    names, closes = (
        (sheet.stocks, sheet.stock_closes) if stock else (sheet.instruments, sheet.closes)
    )
    if symbol not in names:
        what = "a stock the fact sheet's screen" if stock else "an instrument the fact sheet"
        return drop(f"names {answer.symbol}, which is not {what} lists")
    if taken and symbol in taken:
        return drop(f"names {symbol}, which {taken[symbol]} already holds")
    try:
        facts = cited_lines(answer.facts, sheet)
    except ValueError as exc:
        return drop(str(exc))
    close = closes.get(symbol)
    if close is None:
        return drop(f"{symbol} has no close on the fact sheet: its levels rest on no price", facts)
    if not answer.invalidation < close < answer.target:
        return drop(
            f"its invalidation {answer.invalidation} and target {answer.target} do not bracket "
            f"{symbol}'s last close {number(close)}",
            facts,
        )
    if not sheet.as_of < answer.horizon <= last_day:
        return drop(
            f"its horizon {answer.horizon.isoformat()} is not after {sheet.as_of.isoformat()} "
            f"and on or before {last_day.isoformat()}",
            facts,
        )
    return Proposal(seq, text, facts, answer.model_copy(update={"symbol": symbol}), None)


def parse_drafts(
    content: str,
    sheet: FactSheet,
    *,
    wanted: int,
    last_day: date,
    open_theses: Sequence[Thesis] = (),
) -> tuple[Proposal, ...]:
    """The drafts of an answer; the answer itself must be ``{"drafts": [...]}``. A draft on an
    instrument an open thesis holds, or an earlier draft of the same answer names, is dropped."""
    try:
        data = json.loads(content)
    except ValueError as exc:
        raise ValueError(f"the answer is not JSON: {exc}") from None
    if (
        not isinstance(data, dict)
        or set(data) != {"drafts"}
        or not isinstance(data["drafts"], list)
    ):
        raise ValueError('the answer must be one object, {"drafts": [...]}, and nothing else')
    taken = {thesis.instrument.symbol: f"open thesis {thesis.ref}" for thesis in open_theses}
    found = []
    for seq, raw in enumerate(data["drafts"], start=1):
        proposal = check_draft(seq, raw, sheet, wanted=wanted, last_day=last_day, taken=taken)
        if proposal.answer is not None:
            taken[proposal.answer.symbol] = f"draft {seq} of this answer"
        found.append(proposal)
    return tuple(found)


def parse_opinion(content: str, sheet: FactSheet) -> tuple[OpinionAnswer, tuple[FactLine, ...]]:
    answer = OpinionAnswer.model_validate_json(content)
    return answer, cited_lines(answer.facts, sheet)


@dataclass(frozen=True, slots=True)
class AnalystRun:
    """What one run did."""

    state: RunState
    reason: str
    opinions: int = 0
    invalid: int = 0
    failed: int = 0
    #: The drafts call, when one was made, and its drafts.
    drafts_call: AnalystCall | None = None
    proposals: tuple[Proposal, ...] = ()
    cost_usd: float = 0.0


@dataclass(frozen=True, slots=True)
class _Asked[T]:
    call: AnalystCall
    parsed: T | None


class _Tally:
    def __init__(self, spent: float) -> None:
        self.spent = spent
        self.cost = 0.0
        self.opinions = self.invalid = self.failed = 0

    def add(self, call: AnalystCall) -> None:
        self.spent += call.cost_usd
        self.cost += call.cost_usd
        if call.status is ExtractionStatus.INVALID:
            self.invalid += 1
        elif call.status is ExtractionStatus.FAILED:
            self.failed += 1


class Analyst:
    """Writes second opinions and proposes drafts from one fact sheet."""

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
        self._settings = settings
        self._config = llm.analyst
        self._budget = llm.budget
        self._model = llm.analyst_model()
        self._provider_name = self._model.provider
        self._provider = llm.analyst_provider()
        self._chat = TRANSPORTS[self._provider.protocol]
        self._client = client
        self._worker = worker
        self._key = key
        self._now = now
        self._policy = dataclasses.replace(
            RetryPolicy.from_settings(settings.http),
            timeout_seconds=float(self._config.request_timeout_seconds),
        )
        self.prompt_hash = analyst_prompt_hash(settings)

    # ── the run ──

    async def run(self, sheet: FactSheet) -> AnalystRun:
        owed = await self._worker.run(
            analyst_repo.awaiting_opinion,
            as_of=sheet.as_of,
            max_failures=self._config.max_call_failures,
        )
        if self._key is None:
            return AnalystRun(
                "skipped",
                f"{self._provider.key_env} is unset: no call made, no draft proposed, "
                f"{len(owed)} owner ideas await a second opinion",
            )
        tally = _Tally(await self._worker.run(analyst_repo.llm_spend, month_start(self._now())))
        for thesis in owed:
            messages = self._messages(
                OPINION_SYSTEM,
                opinion_prompt(as_of=sheet.as_of, thesis=thesis, facts=sheet.text()),
            )
            if (stopped := self._over_budget(tally, messages)) is not None:
                return stopped
            asked = await self._ask(
                CallKind.OPINION, messages, sheet, lambda c: parse_opinion(c, sheet), thesis
            )
            tally.add(asked.call)
            if asked.parsed is not None:
                answer, facts = asked.parsed
                await self._worker.run(
                    analyst_repo.record_opinion,
                    SecondOpinion(
                        thesis_id=thesis.thesis_id,
                        call_id=asked.call.call_id,
                        probability=answer.probability,
                        critique=answer.critique,
                        facts=json.dumps([line.render() for line in facts]),
                    ),
                )
                tally.opinions += 1
        return await self._drafts(sheet, tally)

    async def _drafts(self, sheet: FactSheet, tally: _Tally) -> AnalystRun:
        monday = sheet.as_of - timedelta(days=sheet.as_of.weekday())
        handed = await self._worker.run(
            analyst_repo.drafts_handed, first=monday, last=monday + timedelta(days=WEEK_DAYS - 1)
        )
        wanted = self._config.max_drafts_per_week - handed
        if wanted <= 0:
            return self._result(
                "ok",
                f"no draft asked: {handed} of this week's {self._config.max_drafts_per_week} "
                "drafts were proposed",
                tally,
            )
        last_day = sheet.as_of + timedelta(days=self._config.max_horizon_days)
        open_theses = await self._worker.run(
            thesis_repo.theses, as_of=sheet.as_of, states=OPEN_STATES
        )
        messages = self._messages(
            DRAFTS_SYSTEM,
            drafts_prompt(
                self._settings,
                as_of=sheet.as_of,
                wanted=wanted,
                last_day=last_day,
                open_theses=open_theses,
                facts=sheet.text(),
            ),
        )
        if (stopped := self._over_budget(tally, messages)) is not None:
            return stopped
        asked = await self._ask(
            CallKind.DRAFTS,
            messages,
            sheet,
            lambda c: parse_drafts(
                c, sheet, wanted=wanted, last_day=last_day, open_theses=open_theses
            ),
            None,
        )
        tally.add(asked.call)
        proposals = asked.parsed or ()
        valid = sum(1 for p in proposals if p.answer is not None)
        return self._result(
            "ok",
            f"{tally.opinions} second opinions written; {len(proposals)} drafts returned, "
            f"{valid} valid (at most {wanted} asked)",
            tally,
            asked.call,
            proposals,
        )

    def _result(
        self,
        state: RunState,
        reason: str,
        tally: _Tally,
        call: AnalystCall | None = None,
        proposals: tuple[Proposal, ...] = (),
    ) -> AnalystRun:
        return AnalystRun(
            state,
            reason,
            tally.opinions,
            tally.invalid,
            tally.failed,
            call,
            proposals,
            tally.cost,
        )

    # ── the budget ──

    def _tokens(self, characters: int) -> int:
        return int(characters / self._budget.chars_per_token_estimate) + 1

    def _cost(self, input_tokens: int, output_tokens: int) -> float:
        model = self._model
        return (
            input_tokens * model.input_usd_per_million
            + output_tokens * model.output_usd_per_million
        ) / TOKENS_PER_PRICE_UNIT

    def _input_tokens(self, messages: Sequence[dict[str, str]]) -> int:
        return self._tokens(sum(len(m["content"]) for m in messages))

    def worst_cost(self, messages: Sequence[dict[str, str]]) -> float:
        """Every attempt at its estimated input and its full output allowance; a retry also
        carries the first answer back."""
        attempts = self._config.retries_on_invalid + 1
        per_call = self._input_tokens(messages) + self._config.max_output_tokens
        return attempts * self._cost(per_call, self._config.max_output_tokens)

    def _over_budget(self, tally: _Tally, messages: list[dict[str, str]]) -> AnalystRun | None:
        worst = self.worst_cost(messages)
        if tally.spent + worst <= self._budget.monthly_usd:
            return None
        return self._result(
            "budget_spent",
            f"the month's LLM spend {tally.spent:.4f} plus a call's worst case {worst:.4f} passes "
            f"{self._budget.monthly_usd:.2f}: no further call this month",
            tally,
        )

    # ── one call ──

    @staticmethod
    def _messages(system: str, user: str) -> list[dict[str, str]]:
        return [{"role": "system", "content": system}, {"role": "user", "content": user}]

    async def _ask[T](
        self,
        kind: CallKind,
        messages: list[dict[str, str]],
        sheet: FactSheet,
        parse: Callable[[str], T],
        thesis: Thesis | None,
    ) -> _Asked[T]:
        """One call, retried once on an invalid answer, stored as produced."""
        assert self._key is not None  # the run checks the key first
        schema = schema_of(DraftsAnswer if kind is CallKind.DRAFTS else OpinionAnswer)
        used_in = used_out = attempts = 0
        problem = ""
        parsed: T | None = None
        output: str | None = None
        status = ExtractionStatus.INVALID
        if self._input_tokens(messages) + self._config.max_output_tokens > (
            self._model.context_window_tokens
        ):
            problem = "the prompt and the answer would not fit the model's context window"
        else:
            for _ in range(self._config.retries_on_invalid + 1):
                attempts += 1
                try:
                    reply = await self._call(messages, schema)
                except Exception as exc:  # the call failed: nothing of it is used
                    status, problem = ExtractionStatus.FAILED, f"{type(exc).__name__}: {exc}"
                    break
                used_in += reply.input_tokens
                used_out += reply.output_tokens
                try:
                    parsed = parse(reply.content)
                except (ValidationError, ValueError) as exc:
                    problem = problems(exc)
                    messages = [
                        *messages,
                        {"role": "assistant", "content": reply.content},
                        {
                            "role": "user",
                            "content": f"That answer is not valid: {problem}. Answer again with "
                            "one JSON object that matches the schema, and nothing else.",
                        },
                    ]
                    continue
                status, output = ExtractionStatus.OK, reply.content
                break
        if status is ExtractionStatus.INVALID:
            log.warning("an analyst answer stayed invalid", extra={"problem": problem})
        call = AnalystCall(
            call_id=UNSTORED_CALL,
            kind=kind,
            thesis_id=None if thesis is None else thesis.thesis_id,
            as_of=sheet.as_of,
            sheet_hash=sheet.sheet_hash,
            produced_at=self._now(),
            provider=self._provider_name,
            model_id=self._model.model_id,
            prompt_version=self._config.prompt_version,
            prompt_hash=self.prompt_hash,
            status=status,
            attempts=attempts,
            output=output,
            error=None if status is ExtractionStatus.OK else problem,
            input_tokens=used_in,
            output_tokens=used_out,
            cost_usd=self._cost(used_in, used_out),
        )
        stored = await self._worker.run(analyst_repo.record_call, call)
        return _Asked(stored, parsed if status is ExtractionStatus.OK else None)

    async def _call(self, messages: list[dict[str, str]], schema: dict[str, object]) -> Reply:
        assert self._key is not None
        return await self._chat(
            self._client,
            base_url=str(self._provider.base_url),
            key=self._key,
            model_id=self._model.model_id,
            messages=messages,
            schema=schema,
            temperature=self._config.temperature,
            max_tokens=self._config.max_output_tokens,
            policy=self._policy,
            worst_input_tokens=self._input_tokens(messages),
        )
