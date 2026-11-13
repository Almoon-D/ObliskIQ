"""The book's sizes and slots (MASTER_PLAN §5.5).

The base size is the book's capital over ``thesis.max_positions``; the owner's conviction
multiplies it (``thesis.conviction``), never past ``thesis.caps.position`` of the book's capital.
A slot is a position or an approved idea; slots are filled in order of arrival among the ideas the
gate passes, and a slot without a thesis holds nothing.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from dataclasses import dataclass
from decimal import Decimal

from ...config.schema_thesis import ThesisBookConfig
from ...domain.thesis import Conviction


@dataclass(frozen=True, slots=True)
class Slot:
    """One of the book's slots in use: what it commits of the book's capital, and what the caps
    count it under (``None``: nothing states it, so no cap counts it)."""

    conid: int
    size_base: Decimal
    sector: str | None
    country: str | None
    currency: str | None


def base_size(capital: Decimal, thesis: ThesisBookConfig) -> Decimal:
    """What a normal idea takes of ``capital``."""
    return capital / thesis.max_positions


def position_size(capital: Decimal, conviction: Conviction, thesis: ThesisBookConfig) -> Decimal:
    """What an idea of ``conviction`` takes of ``capital``: the base size times the conviction's
    multiple, inside the position cap."""
    multiple = {
        Conviction.NORMAL: thesis.conviction.normal,
        Conviction.HIGH: thesis.conviction.high,
        Conviction.MAX: thesis.conviction.max,
    }[conviction]
    return min(base_size(capital, thesis) * multiple, thesis.caps.position * capital)
