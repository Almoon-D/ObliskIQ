"""What the LLM analyst leaves in the store (MASTER_PLAN §5.5, §5.10; DECISIONS D128, D156).

A **call** is one exchange with the model, for drafts of its own or for a second opinion on one
owner thesis, stamped with the provider, the model, the prompt's version and hash (its segment of
the forward record), the fact sheet it read, its tokens and its cost. A **draft record** is one
draft a call returned and what became of it. A **second opinion** is a critique and the stated
probability that an owner's thesis reaches its target: logged and graded, never a veto.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from datetime import date
from enum import StrEnum
from typing import Annotated, Final, Self

from pydantic import Field, NonNegativeFloat, NonNegativeInt, PositiveInt, model_validator

from .base import DomainModel, NonEmptyText, UtcDatetime
from .sources import ExtractionStatus, Probability

#: The id of a call the store has not numbered yet.
UNSTORED_CALL: Final = 0
#: How many characters of a prompt hash name a segment to a reader.
SEGMENT_HASH_CHARS: Final = 12


class CallKind(StrEnum):
    DRAFTS = "drafts"
    OPINION = "opinion"


def segment_label(model_id: str, prompt_hash: str) -> str:
    """How a segment (one model, one prompt hash) is named to the owner."""
    return f"{model_id} {prompt_hash[:SEGMENT_HASH_CHARS]}"


class AnalystCall(DomainModel):
    """One call of the analyst, as produced."""

    call_id: Annotated[int, Field(ge=0)]
    kind: CallKind
    #: The owner thesis a second opinion is on; ``None`` for drafts.
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

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if (self.kind is CallKind.OPINION) != (self.thesis_id is not None):
            raise ValueError("a second opinion names its thesis, and only it does")
        if (self.status is ExtractionStatus.OK) != (self.output is not None):
            raise ValueError("a call holds an output exactly when it is ok")
        if self.status is not ExtractionStatus.OK and not self.error:
            raise ValueError("a call that is not ok says why")
        return self

    @property
    def segment(self) -> str:
        return segment_label(self.model_id, self.prompt_hash)


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


class OpinionScore(DomainModel):
    """The second opinions of one segment whose theses have closed: their count and mean Brier
    score (the squared distance of the stated probability from what happened)."""

    segment: NonEmptyText
    graded: PositiveInt
    brier: Annotated[float, Field(ge=0, le=1)]


def brier(probability: float, reached: bool) -> float:
    """One forecast's Brier score: 0 is a certain and right call, 1 a certain and wrong one."""
    return (probability - (1.0 if reached else 0.0)) ** 2
