"""What the EventExtractor produces from a news item (MASTER_PLAN §5.10; DECISIONS D70).

One market-relevant event per item: the entities named, the region and sector it bears on, its
type, its tone for the entities named and how new it is, and the source ids it rests on (the
item's own, so an answer that cites anything else is invalid). Nothing consumes it yet: H7's
pre-registration (research/preregistrations/llm-events.md) tests whether it adds to the price
signal, on what is produced forward from the first live run.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from typing import Annotated, Literal

from pydantic import Field

from ..domain.base import DomainModel, NonEmptyText
from ..domain.sources import Probability, Tone

#: The kinds of event an item may report.
type EventType = Literal[
    "monetary_policy",
    "macro_release",
    "regulation",
    "enforcement",
    "earnings",
    "guidance",
    "operations",
    "corporate_action",
    "management",
    "legal",
    "geopolitics",
    "other",
]

#: The most entities an event names (a longer list is a summary, not an event).
MAX_ENTITIES = 20


class ExtractedEvent(DomainModel):
    """One event read from one news item."""

    entities: Annotated[list[NonEmptyText], Field(max_length=MAX_ENTITIES)]
    region: NonEmptyText | None
    sector: NonEmptyText | None
    type: EventType
    tone: Tone
    novelty: Probability
    sources: Annotated[list[NonEmptyText], Field(min_length=1)]
