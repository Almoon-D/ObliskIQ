"""What the LLM analyst leaves in the store (MASTER_PLAN §5.5, §5.10; DECISIONS D128, D156).

A **call** is one exchange with the model: a scan of the sheet for candidates, a dossier that
writes one candidate's thesis, drafts of its own (the single pass before segments, D188), a second
opinion on one owner thesis or a weekly review of one open thesis. Each is stamped with its
segment (the configured name), the provider, the model, the prompt's version and hash, the fact
sheet it read, its tokens and its cost. A **draft record** is one draft a call returned and what
became of it. A **second opinion** and a **review** are a critique and the stated probability that
a thesis reaches its target: logged and graded, never a veto, never a change to the thesis.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from datetime import date
from enum import StrEnum
from typing import Annotated, Final, Literal, Self

from pydantic import Field, NonNegativeFloat, NonNegativeInt, PositiveInt, model_validator

from .base import DomainModel, NonEmptyText, UtcDatetime
from .sources import ExtractionStatus, Probability

#: The id of a call the store has not numbered yet.
UNSTORED_CALL: Final = 0
#: How many characters of a prompt hash name a segment to a reader.
SEGMENT_HASH_CHARS: Final = 12


class CallKind(StrEnum):
    SCAN = "scan"
    DOSSIER = "dossier"
    #: The single drafts pass of the analyst before segments: read, never written.
    DRAFTS = "drafts"
    OPINION = "opinion"
    REVIEW = "review"


#: The record a call belongs to: the forward one, or the replay after the cutoff (H15).
type CallRecord = Literal["forward", "replay"]
FORWARD: Final = "forward"
REPLAY: Final = "replay"

#: The calls that are on one thesis.
ON_A_THESIS: Final = frozenset({CallKind.OPINION, CallKind.REVIEW})


def segment_label(name: str, prompt_hash: str) -> str:
    """How a segment is named to the owner: its configured name (or, for a call made before
    segments, its model id) and its prompt hash."""
    return f"{name} {prompt_hash[:SEGMENT_HASH_CHARS]}"


class AnalystCall(DomainModel):
    """One call of the analyst, as produced."""

    call_id: Annotated[int, Field(ge=0)]
    kind: CallKind
    #: The thesis a second opinion or a review is on; ``None`` for the other kinds.
    thesis_id: PositiveInt | None
    as_of: date
    sheet_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    produced_at: UtcDatetime
    provider: NonEmptyText
    model_id: NonEmptyText
    prompt_version: PositiveInt
    prompt_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    status: ExtractionStatus
    attempts: NonNegativeInt
    output: str | None
    error: str | None
    input_tokens: NonNegativeInt
    output_tokens: NonNegativeInt
    cost_usd: NonNegativeFloat
    #: The configured segment that made it; ``None`` for a call made before segments.
    segment: NonEmptyText | None = None
    #: The record it belongs to: ``forward`` (shadow, paper or live), or ``replay`` (H15), which
    #: is never pooled with it.
    record: CallRecord = "forward"

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if (self.kind in ON_A_THESIS) != (self.thesis_id is not None):
            raise ValueError("a second opinion or a review names its thesis, and only they do")
        if (self.status is ExtractionStatus.OK) != (self.output is not None):
            raise ValueError("a call holds an output exactly when it is ok")
        if self.status is not ExtractionStatus.OK and not self.error:
            raise ValueError("a call that is not ok says why")
        return self

    @property
    def label(self) -> str:
        """The segment as the record names it."""
        return segment_label(self.segment or self.model_id, self.prompt_hash)


class DraftOutcome(StrEnum):
    #: The analyst's own checks dropped it (no fact cited, an unknown instrument, a field
    #: missing, a fact dated after the sheet): it never reached the intake.
    DROPPED = "dropped"
    #: The intake refused it, with its reasons.
    REFUSED = "refused"
    #: Stored as a candidate thesis; the book's gate decides.
    ACCEPTED = "accepted"


class DraftRecord(DomainModel):
    """One draft of a drafts call and what became of it."""

    call_id: PositiveInt
    seq: PositiveInt
    #: The draft as the model wrote it (JSON text).
    draft: NonEmptyText
    #: The fact lines it cited, as they read on the sheet (JSON text).
    facts: str
    outcome: DraftOutcome
    reason: NonEmptyText | None
    thesis_id: PositiveInt | None

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if (self.outcome is DraftOutcome.ACCEPTED) != (self.thesis_id is not None):
            raise ValueError("an accepted draft names its thesis, and only it does")
        if self.outcome is not DraftOutcome.ACCEPTED and self.reason is None:
            raise ValueError("a draft that is not accepted says why")
        return self


class SecondOpinion(DomainModel):
    """The analyst's opinion of one owner thesis, from one ok call."""

    thesis_id: PositiveInt
    call_id: PositiveInt
    probability: Probability
    critique: NonEmptyText
    #: The fact lines it cited, as they read on the sheet (JSON text).
    facts: str


class ThesisReview(DomainModel):
    """A segment's weekly review of one open thesis or open call, from one ok call: its critique
    and the probability it now states that the target comes first. Changes nothing."""

    call_id: PositiveInt
    thesis_id: PositiveInt
    probability: Probability
    critique: NonEmptyText
    #: The fact lines it cited, as they read on the sheet (JSON text).
    facts: str


class OpinionScore(DomainModel):
    """The second opinions of one segment whose theses have closed: their count and mean Brier
    score (the squared distance of the stated probability from what happened)."""

    segment: NonEmptyText
    graded: PositiveInt
    brier: Annotated[float, Field(ge=0, le=1)]


def brier(probability: float, reached: bool) -> float:
    """One forecast's Brier score: 0 is a certain and right call, 1 a certain and wrong one."""
    return (probability - (1.0 if reached else 0.0)) ** 2
