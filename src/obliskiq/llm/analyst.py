"""The Analyst role (MASTER_PLAN §5.5, §5.10; DECISIONS D156, D188): theses of its own from the
fact sheet, a second opinion on each of the owner's ideas and a weekly review of what is open, run
as **segments** side by side, every call stored as produced.

A segment (``llm.analyst.segments``) is a scan model, a dossier model, a prompt version and a share
of the one monthly budget; its identity in the record is its name and its prompt's hash (D128). A
run of one segment reads one fact sheet (``grid/facts.py``) and:

1. if it is the **trading segment**, writes a **second opinion** on each of the owner's open theses
   that has none (``store/analyst.py: awaiting_opinion``), on its dossier model: a critique and the
   probability it states that the target is reached. Logged and graded, never a veto;
2. **scans** the sheet on its scan model for at most ``scan.max_candidates`` instruments or screen
   stocks, each with one line of reason and the fact lines it rests on. A candidate is **dropped**
   (logged) when a field is missing, its name is not in the section it names, an open thesis of
   the owner or of the segment (or an earlier candidate) holds it, or it cites a line the sheet does
   not hold or one dated after it. No scan is made when the segment's drafts this ISO week reached
   ``max_drafts_per_week``;
3. writes a **dossier** per candidate on its dossier model, in the scan's order, until it holds as
   many valid drafts as it may propose (``max_drafts_per_run`` and what is left of the week): the
   thesis as today's draft plus its probability, from the sheet's lines for that candidate (those
   naming it, those the scan cited and the ``dossier.context_sections``). Proposing none is valid.
   A draft is checked as before (``check_draft``) and dropped, with its reason, when it names
   another instrument than its candidate. What survives is a :class:`Proposal` the service hands to
   the thesis book's intake as source ``analyst``; the intake and the gate decide, and only the
   trading segment's theses may take a slot (the rebalancer reads the segment);
4. **reviews**, on its dossier model, each open thesis of the owner and each of its own open theses
   or open calls not reviewed by it this ISO week, at most ``review.max_theses_per_week``: a
   critique and a probability, stored and graded like a second opinion. A review changes no thesis
   and triggers no exit: nothing on the order path reads it.

Each call asks for JSON matching a schema, and is asked once more with the validation error when
its answer is not that JSON (``retries_on_invalid``); then it fails closed (``invalid``) and nothing
of it is used. A call that fails outright (``failed``) uses nothing. A segment whose provider has no
key makes no call (``no_key``); nothing falls back to another model. A call is made only if the
month's spend on every LLM role plus its worst case stays within ``budget.monthly_usd`` and the
segment's spend plus its worst case within its share of it; otherwise the segment stops
(``budget``) and the thesis book runs on its exits. Nothing here sizes, orders or gates: ``llm``
imports nothing from ``decision`` or ``execution``.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import dataclasses
import functools
import json
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Literal

import httpx
from pydantic import ValidationError

from ..config.schema import ObliskConfig
from ..config.schema_llm import LlmModelConfig, LlmProviderConfig
from ..domain.analyst import (
    FORWARD,
    UNSTORED_CALL,
    AnalystCall,
    CallKind,
    CallRecord,
    SecondOpinion,
    ThesisReview,
    segment_label,
)
from ..domain.sources import ExtractionStatus
from ..domain.thesis import OPEN_STATES, Expression, Thesis, ThesisDraft, ThesisSource
from ..grid.facts import SECTIONS, FactLine, FactSheet, number
from ..logging_setup import get_logger
from ..store import analyst as analyst_repo
from ..store import theses as thesis_repo
from ..store.worker import StoreWorker
from ..util.http import RetryPolicy
from .analyst_prompts import (
    DOSSIER_SYSTEM,
    FILINGS_SYSTEM,
    OPINION_SYSTEM,
    REVIEW_SYSTEM,
    SCAN_SYSTEM,
    STOCK_SECTION,
    DraftAnswer,
    DraftsAnswer,
    OpinionAnswer,
    ScanAnswer,
    ScanCandidate,
    analyst_prompt_hash,
    describe_candidate,
    dossier_prompt,
    filings_prompt,
    opinion_prompt,
    reads_filings,
    scan_prompt,
    schema_of,
)
from .extractor import SHOWN_PROBLEMS, TOKENS_PER_PRICE_UNIT, month_start
from .transport import TRANSPORTS, Reply

log = get_logger("llm.analyst")

type RunState = Literal["ok", "no_key", "budget"]
#: Reads a candidate's own filings for its dossier, as of a day: one line per section, or why
#: there are none (``data/events/filing_text.py``).
type Texts = Callable[[str, date], Awaitable[list[str]]]
#: The line a dossier reads when its run has no reader of the filings.
NO_READER = "not read: this run has no reader of the SEC's filings"
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


def week_of(day: date) -> tuple[date, date]:
    """The ISO week (Monday to Sunday) ``day`` falls in."""
    monday = day - timedelta(days=day.weekday())
    return monday, monday + timedelta(days=WEEK_DAYS - 1)


def names(symbol: str) -> re.Pattern[str]:
    """A line names ``symbol`` when it appears as a word of its own."""
    return re.compile(rf"(?<![A-Za-z0-9.]){re.escape(symbol)}(?![A-Za-z0-9])")


def lines_for(
    sheet: FactSheet, symbol: str, cited: Sequence[str], sections: Sequence[str]
) -> FactSheet:
    """The sheet as one candidate's dossier reads it: the lines naming ``symbol``, the lines
    ``cited`` and every line of ``sections``, with their own references and the sheet's names
    and closes (a draft's levels are judged against them)."""
    pattern = names(symbol)
    wanted = {ref.strip().upper() for ref in cited}
    kept = tuple(
        line
        for line in sheet.lines
        if line.ref in wanted or line.section in sections or pattern.search(line.text)
    )
    return dataclasses.replace(sheet, lines=kept)


@dataclass(frozen=True, slots=True)
class Proposal:
    """One draft of a dossier call: valid (``answer``) or dropped (``dropped``, why)."""

    seq: int
    raw: str
    facts: tuple[FactLine, ...]
    answer: DraftAnswer | None
    dropped: str | None
    #: The stored call it came from (``UNSTORED_CALL`` until the run stores it).
    call_id: int = UNSTORED_CALL

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
            probability=answer.probability,
        )


def check_draft(
    seq: int,
    raw: object,
    sheet: FactSheet,
    *,
    wanted: int,
    last_day: date,
    taken: Mapping[str, str] | None = None,
    candidate: str | None = None,
) -> Proposal:
    """One draft as written, checked against the sheet; ``taken`` names the instruments an open
    thesis or an earlier draft already holds (symbol to what holds it), ``candidate`` the one
    symbol a dossier may name."""
    text = json.dumps(raw, sort_keys=True, default=str)

    def drop(reason: str, facts: tuple[FactLine, ...] = ()) -> Proposal:
        log.warning("an analyst draft is dropped", extra={"seq": seq, "reason": reason})
        return Proposal(seq, text, facts, None, reason)

    if seq > wanted:
        return drop(f"beyond the {wanted} drafts asked for")
    try:
        answer = DraftAnswer.model_validate(raw)
    except ValidationError as exc:
        return drop(f"a field is missing or wrong: {problems(exc)}")
    symbol = answer.symbol.strip().upper()
    if candidate is not None and symbol != candidate:
        return drop(f"names {answer.symbol}, not its candidate {candidate}")
    stock = answer.section == STOCK_SECTION
    listed, closes = (
        (sheet.stocks, sheet.stock_closes) if stock else (sheet.instruments, sheet.closes)
    )
    if symbol not in listed:
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


def _envelope(content: str, key: str) -> list[object]:
    try:
        data = json.loads(content)
    except ValueError as exc:
        raise ValueError(f"the answer is not JSON: {exc}") from None
    if not isinstance(data, dict) or set(data) != {key} or not isinstance(data[key], list):
        raise ValueError(f'the answer must be one object, {{"{key}": [...]}}, and nothing else')
    found: list[object] = data[key]
    return found


def taken_by(open_theses: Sequence[Thesis]) -> dict[str, str]:
    return {thesis.instrument.symbol: f"open thesis {thesis.ref}" for thesis in open_theses}


def parse_drafts(
    content: str,
    sheet: FactSheet,
    *,
    wanted: int,
    last_day: date,
    open_theses: Sequence[Thesis] = (),
    candidate: str | None = None,
) -> tuple[Proposal, ...]:
    """The drafts of an answer; the answer itself must be ``{"drafts": [...]}``. A draft on an
    instrument an open thesis holds, or an earlier draft of the same answer names, is dropped."""
    taken = taken_by(open_theses)
    found = []
    for seq, raw in enumerate(_envelope(content, "drafts"), start=1):
        proposal = check_draft(
            seq, raw, sheet, wanted=wanted, last_day=last_day, taken=taken, candidate=candidate
        )
        if proposal.answer is not None:
            taken[proposal.answer.symbol] = f"draft {seq} of this answer"
        found.append(proposal)
    return tuple(found)


def check_candidate(raw: object, sheet: FactSheet, taken: Mapping[str, str]) -> ScanCandidate | str:
    """One candidate of a scan, normalised, or why it is dropped."""
    try:
        candidate = ScanCandidate.model_validate(raw)
    except ValidationError as exc:
        return f"a field is missing or wrong: {problems(exc)}"
    symbol = candidate.symbol.strip().upper()
    listed = sheet.stocks if candidate.section == STOCK_SECTION else sheet.instruments
    if symbol not in listed:
        return f"names {candidate.symbol}, which is not in the sheet's {candidate.section} section"
    if symbol in taken:
        return f"names {symbol}, which {taken[symbol]} already holds"
    try:
        cited_lines(candidate.facts, sheet)
    except ValueError as exc:
        return str(exc)
    return candidate.model_copy(update={"symbol": symbol})


def parse_scan(
    content: str, sheet: FactSheet, *, wanted: int, open_theses: Sequence[Thesis] = ()
) -> tuple[ScanCandidate, ...]:
    """The valid candidates of a scan's answer, at most ``wanted``, in the scan's order; the
    answer itself must be ``{"candidates": [...]}``. A bad candidate is dropped and logged."""
    taken = taken_by(open_theses)
    kept: list[ScanCandidate] = []
    for seq, raw in enumerate(_envelope(content, "candidates"), start=1):
        if len(kept) >= wanted:
            log.warning("a scan names more candidates than asked", extra={"seq": seq})
            break
        found = check_candidate(raw, sheet, taken)
        if isinstance(found, str):
            log.warning("a scan candidate is dropped", extra={"seq": seq, "reason": found})
            continue
        taken[found.symbol] = f"candidate {seq} of this scan"
        kept.append(found)
    return tuple(kept)


def parse_opinion(content: str, sheet: FactSheet) -> tuple[OpinionAnswer, tuple[FactLine, ...]]:
    answer = OpinionAnswer.model_validate_json(content)
    return answer, cited_lines(answer.facts, sheet)


def _parse_on(sheet: FactSheet, content: str) -> tuple[OpinionAnswer, tuple[FactLine, ...]]:
    return parse_opinion(content, sheet)


@dataclass(frozen=True, slots=True)
class AnalystRun:
    """What one segment's run did."""

    state: RunState
    reason: str
    segment: str = ""
    label: str = ""
    opinions: int = 0
    reviews: int = 0
    candidates: int = 0
    invalid: int = 0
    failed: int = 0
    #: The dossier calls made, in order, and their drafts.
    calls: tuple[AnalystCall, ...] = ()
    proposals: tuple[Proposal, ...] = ()
    cost_usd: float = 0.0


@dataclass(frozen=True, slots=True)
class _Asked[T]:
    call: AnalystCall
    parsed: T | None


@dataclass(slots=True)
class _Tally:
    spent: float
    segment_spent: float
    cost: float = 0.0
    opinions: int = 0
    reviews: int = 0
    candidates: int = 0
    invalid: int = 0
    failed: int = 0
    calls: list[AnalystCall] = field(default_factory=list)
    proposals: list[Proposal] = field(default_factory=list)

    def add(self, call: AnalystCall) -> None:
        self.spent += call.cost_usd
        self.segment_spent += call.cost_usd
        self.cost += call.cost_usd
        if call.status is ExtractionStatus.INVALID:
            self.invalid += 1
        elif call.status is ExtractionStatus.FAILED:
            self.failed += 1


class _Stopped(Exception):
    """The budget stops the segment's run."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class _Pass:
    """One pass's model, its provider and its output allowance."""

    model: LlmModelConfig
    provider: LlmProviderConfig
    max_output_tokens: int


class Analyst:
    """One segment: writes second opinions (the trading segment), scans, writes dossiers and
    reviews from one fact sheet."""

    def __init__(
        self,
        settings: ObliskConfig,
        *,
        segment: str,
        client: httpx.AsyncClient,
        worker: StoreWorker,
        keys: Mapping[str, str | None],
        now: Callable[[], datetime],
        sections: Sequence[str] = SECTIONS,
        record: CallRecord = FORWARD,
        texts: Texts | None = None,
    ) -> None:
        llm = settings.llm
        config = llm.analyst
        chosen = config.segments[segment]
        self._settings = settings
        self._config = config
        self._budget = llm.budget
        self.segment = segment
        self.trading = segment == config.trading_segment
        self.share = float(chosen.budget_share) * self._budget.monthly_usd
        self._prompt_version = chosen.prompt_version
        self._scan = _Pass(
            llm.models[chosen.scan_model],
            llm.provider_of(chosen.scan_model),
            config.scan.max_output_tokens,
        )
        self._dossier = _Pass(
            llm.models[chosen.dossier_model],
            llm.provider_of(chosen.dossier_model),
            config.dossier.max_output_tokens,
        )
        self._review = dataclasses.replace(
            self._dossier, max_output_tokens=config.review.max_output_tokens
        )
        self._providers = {
            llm.models[chosen.scan_model].provider: self._scan.provider,
            llm.models[chosen.dossier_model].provider: self._dossier.provider,
        }
        self._keys = keys
        self._client = client
        self._worker = worker
        self._now = now
        self._policy = dataclasses.replace(
            RetryPolicy.from_settings(settings.http),
            timeout_seconds=float(config.request_timeout_seconds),
        )
        #: The sheet's sections are in the hash: a replay segment is never the forward one (H15).
        self.prompt_hash = analyst_prompt_hash(settings, segment, sections=sections)
        self.label = segment_label(segment, self.prompt_hash)
        self.record = record
        #: A dossier reads its candidate's filings with the sheet's events (D200).
        self._filings = reads_filings(sections)
        self._texts = texts

    def _key(self, step: _Pass) -> str:
        key = self._keys.get(step.model.provider)
        assert key is not None  # the run checks every key first
        return key

    # ── the run ──

    async def run(self, sheet: FactSheet) -> AnalystRun:
        unset = sorted(
            provider.key_env
            for name, provider in self._providers.items()
            if self._keys.get(name) is None
        )
        if unset:
            return AnalystRun(
                "no_key",
                f"{', '.join(unset)} unset: segment {self.segment} makes no call",
                self.segment,
                self.label,
            )
        start = month_start(self._now())
        tally = _Tally(
            spent=await self._worker.run(analyst_repo.llm_spend, start),
            segment_spent=await self._worker.run(analyst_repo.segment_spend, self.segment, start),
        )
        try:
            if self.trading:
                await self._opinions(sheet, tally)
            drafts = await self._drafts(sheet, tally)
            reviewed = await self._reviews(sheet, tally)
        except _Stopped as stopped:
            return self._result("budget", stopped.reason, tally)
        opinions = f"{tally.opinions} second opinions written; " if self.trading else ""
        return self._result("ok", f"{opinions}{drafts}; {reviewed}", tally)

    def _result(self, state: RunState, reason: str, tally: _Tally) -> AnalystRun:
        return AnalystRun(
            state,
            reason,
            self.segment,
            self.label,
            tally.opinions,
            tally.reviews,
            tally.candidates,
            tally.invalid,
            tally.failed,
            tuple(tally.calls),
            tuple(tally.proposals),
            tally.cost,
        )

    async def _opinions(self, sheet: FactSheet, tally: _Tally) -> None:
        owed = await self._worker.run(
            analyst_repo.awaiting_opinion,
            as_of=sheet.as_of,
            max_failures=self._config.max_call_failures,
        )
        for thesis in owed:
            messages = self._messages(
                OPINION_SYSTEM,
                opinion_prompt(as_of=sheet.as_of, thesis=thesis, facts=sheet.text()),
            )
            asked = await self._ask(
                CallKind.OPINION,
                self._dossier,
                messages,
                sheet,
                lambda c: parse_opinion(c, sheet),
                OpinionAnswer,
                tally,
                thesis,
            )
            if asked.parsed is not None:
                answer, facts = asked.parsed
                await self._worker.run(
                    analyst_repo.record_opinion,
                    SecondOpinion(
                        thesis_id=thesis.thesis_id,
                        call_id=asked.call.call_id,
                        probability=answer.probability,
                        critique=answer.critique,
                        facts=facts_json(facts),
                    ),
                )
                tally.opinions += 1

    async def _open_for_segment(self, as_of: date) -> list[Thesis]:
        """The open theses a segment's drafts may not name: the owner's and its own."""
        known = await self._worker.run(thesis_repo.theses, as_of=as_of, states=OPEN_STATES)
        mine = await self._worker.run(analyst_repo.thesis_segment_names)
        return [
            t
            for t in known
            if t.source is ThesisSource.OWNER or mine.get(t.thesis_id) == self.segment
        ]

    async def _drafts(self, sheet: FactSheet, tally: _Tally) -> str:
        first, last = week_of(sheet.as_of)
        handed = await self._worker.run(
            analyst_repo.drafts_handed, first=first, last=last, segment=self.segment
        )
        wanted = min(self._config.max_drafts_per_week - handed, self._config.max_drafts_per_run)
        if wanted <= 0:
            return (
                f"no scan: {handed} of this week's {self._config.max_drafts_per_week} drafts "
                f"were proposed, at most {self._config.max_drafts_per_run} a run"
            )
        open_theses = await self._open_for_segment(sheet.as_of)
        scan_wanted = self._config.scan.max_candidates
        messages = self._messages(
            SCAN_SYSTEM,
            scan_prompt(
                as_of=sheet.as_of, wanted=scan_wanted, open_theses=open_theses, facts=sheet.text()
            ),
        )
        scanned = await self._ask(
            CallKind.SCAN,
            self._scan,
            messages,
            sheet,
            lambda c: parse_scan(c, sheet, wanted=scan_wanted, open_theses=open_theses),
            ScanAnswer,
            tally,
        )
        candidates = scanned.parsed or ()
        tally.candidates += len(candidates)
        last_day = sheet.as_of + timedelta(days=self._config.max_horizon_days)
        valid = 0
        for candidate in candidates:
            if valid >= wanted:
                break
            valid += await self._write_dossier(sheet, candidate, open_theses, last_day, tally)
        return (
            f"{len(candidates)} candidates scanned, {valid} valid drafts written (at most {wanted})"
        )

    async def _write_dossier(
        self,
        sheet: FactSheet,
        candidate: ScanCandidate,
        open_theses: Sequence[Thesis],
        last_day: date,
        tally: _Tally,
    ) -> int:
        facts = lines_for(
            sheet, candidate.symbol, candidate.facts, self._config.dossier.context_sections
        )
        system = DOSSIER_SYSTEM
        user = dossier_prompt(
            self._settings,
            as_of=sheet.as_of,
            last_day=last_day,
            open_theses=open_theses,
            candidate=describe_candidate(candidate.section, candidate.symbol, candidate.reason),
            facts=facts.text(),
        )
        if self._filings:
            read = (
                [NO_READER]
                if self._texts is None
                else await self._texts(candidate.symbol, sheet.as_of)
            )
            system = f"{DOSSIER_SYSTEM} {FILINGS_SYSTEM}"
            user = f"{user}\n{filings_prompt(read)}"
        messages = self._messages(system, user)
        asked = await self._ask(
            CallKind.DOSSIER,
            self._dossier,
            messages,
            facts,
            lambda c: parse_drafts(
                c,
                facts,
                wanted=1,
                last_day=last_day,
                open_theses=open_theses,
                candidate=candidate.symbol,
            ),
            DraftsAnswer,
            tally,
        )
        tally.calls.append(asked.call)
        proposals = [dataclasses.replace(p, call_id=asked.call.call_id) for p in asked.parsed or ()]
        tally.proposals += proposals
        return sum(1 for p in proposals if p.answer is not None)

    async def _reviews(self, sheet: FactSheet, tally: _Tally) -> str:
        cap = self._config.review.max_theses_per_week
        first, last = week_of(sheet.as_of)
        done = await self._worker.run(
            analyst_repo.reviewed, segment=self.segment, first=first, last=last
        )
        targets = await self._worker.run(
            analyst_repo.review_targets, segment=self.segment, as_of=sheet.as_of
        )
        owed = [t for t in targets if t.thesis_id not in done][: max(0, cap - len(done))]
        for thesis in owed:
            facts = lines_for(
                sheet, thesis.instrument.symbol, (), self._config.dossier.context_sections
            )
            messages = self._messages(
                REVIEW_SYSTEM,
                opinion_prompt(as_of=sheet.as_of, thesis=thesis, facts=facts.text()),
            )
            asked = await self._ask(
                CallKind.REVIEW,
                self._review,
                messages,
                facts,
                functools.partial(_parse_on, facts),
                OpinionAnswer,
                tally,
                thesis,
            )
            if asked.parsed is not None:
                answer, cited = asked.parsed
                await self._worker.run(
                    analyst_repo.record_review,
                    ThesisReview(
                        call_id=asked.call.call_id,
                        thesis_id=thesis.thesis_id,
                        probability=answer.probability,
                        critique=answer.critique,
                        facts=facts_json(cited),
                    ),
                )
                tally.reviews += 1
        return f"{tally.reviews} reviews written ({len(done) + len(owed)} of {cap} this week)"

    # ── the budget ──

    def _tokens(self, characters: int) -> int:
        return int(characters / self._budget.chars_per_token_estimate) + 1

    @staticmethod
    def _cost(model: LlmModelConfig, input_tokens: int, output_tokens: int) -> float:
        return (
            input_tokens * model.input_usd_per_million
            + output_tokens * model.output_usd_per_million
        ) / TOKENS_PER_PRICE_UNIT

    def _input_tokens(self, messages: Sequence[dict[str, str]]) -> int:
        return self._tokens(sum(len(m["content"]) for m in messages))

    def worst_cost(self, step: _Pass, messages: Sequence[dict[str, str]]) -> float:
        """Every attempt at its estimated input and its full output allowance; a retry also
        carries the first answer back."""
        attempts = self._config.retries_on_invalid + 1
        per_call = self._input_tokens(messages) + step.max_output_tokens
        return attempts * self._cost(step.model, per_call, step.max_output_tokens)

    def _check_budget(self, step: _Pass, tally: _Tally, messages: list[dict[str, str]]) -> None:
        worst = self.worst_cost(step, messages)
        if tally.spent + worst > self._budget.monthly_usd:
            raise _Stopped(
                f"the month's LLM spend {tally.spent:.4f} plus a call's worst case {worst:.4f} "
                f"passes {self._budget.monthly_usd:.2f}: no further call this month"
            )
        if tally.segment_spent + worst > self.share:
            raise _Stopped(
                f"segment {self.segment}'s spend {tally.segment_spent:.4f} plus a call's worst "
                f"case {worst:.4f} passes its share {self.share:.2f}: no further call this month"
            )

    # ── one call ──

    @staticmethod
    def _messages(system: str, user: str) -> list[dict[str, str]]:
        return [{"role": "system", "content": system}, {"role": "user", "content": user}]

    async def _ask[T](
        self,
        kind: CallKind,
        step: _Pass,
        messages: list[dict[str, str]],
        sheet: FactSheet,
        parse: Callable[[str], T],
        answer: type[OpinionAnswer | ScanAnswer | DraftsAnswer],
        tally: _Tally,
        thesis: Thesis | None = None,
    ) -> _Asked[T]:
        """One call, inside the budget, retried once on an invalid answer, stored as produced."""
        self._check_budget(step, tally, messages)
        schema = schema_of(answer)
        used_in = used_out = attempts = 0
        problem = ""
        parsed: T | None = None
        output: str | None = None
        status = ExtractionStatus.INVALID
        if self._input_tokens(messages) + step.max_output_tokens > (
            step.model.context_window_tokens
        ):
            problem = "the prompt and the answer would not fit the model's context window"
        else:
            for _ in range(self._config.retries_on_invalid + 1):
                attempts += 1
                try:
                    reply = await self._call(step, messages, schema)
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
            provider=step.model.provider,
            model_id=step.model.model_id,
            prompt_version=self._prompt_version,
            prompt_hash=self.prompt_hash,
            status=status,
            attempts=attempts,
            output=output,
            error=None if status is ExtractionStatus.OK else problem,
            input_tokens=used_in,
            output_tokens=used_out,
            cost_usd=self._cost(step.model, used_in, used_out),
            segment=self.segment,
            record=self.record,
        )
        stored = await self._worker.run(analyst_repo.record_call, call)
        tally.add(stored)
        return _Asked(stored, parsed if status is ExtractionStatus.OK else None)

    async def _call(
        self, step: _Pass, messages: list[dict[str, str]], schema: dict[str, object]
    ) -> Reply:
        return await TRANSPORTS[step.provider.protocol](
            self._client,
            base_url=str(step.provider.base_url),
            key=self._key(step),
            model_id=step.model.model_id,
            messages=messages,
            schema=schema,
            temperature=self._config.temperature,
            max_tokens=step.max_output_tokens,
            policy=self._policy,
            worst_input_tokens=self._input_tokens(messages),
            api_version=step.provider.api_version,
            effort=step.model.effort,
        )
