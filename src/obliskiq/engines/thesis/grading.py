"""Grading a closed thesis against what it wrote, and the record per source, analyst segment and
conviction (MASTER_PLAN §5.5); the analyst's second opinions graded by their Brier score; and
each option a thesis was held as beside its twin in the underlying, per direction (H8's forward
record, DECISIONS D157).

A grade says whether the target was reached, the payoff realised against the one stated, and the
days held against the horizon. It describes the calls and moves no capital: the book's capital
follows the pre-registered forward record (``research/preregistrations/thesis-book.md``), which
Session 18's scoreboard computes from the same fills.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from ...domain.analyst import AnalystCall, OpinionScore, SecondOpinion, brier
from ...domain.enums import OptionRight
from ...domain.options import PairGrade
from ...domain.orders import Fill, OrderSide
from ...domain.thesis import Expression, Thesis, ThesisGrade, ThesisSource
from .exits import TARGET

ZERO = Decimal(0)


def _average(fills: Sequence[Fill]) -> tuple[Decimal, Decimal]:
    quantity = sum((fill.quantity for fill in fills), ZERO)
    return quantity, sum((fill.quantity * fill.price for fill in fills), ZERO) / quantity


def grade(thesis: Thesis, fills: Sequence[Fill], *, exit_reason: str, at: datetime) -> ThesisGrade:
    """The grade of ``thesis`` from the fills of its position, closed for ``exit_reason``."""
    buys = [fill for fill in fills if fill.side is OrderSide.BUY]
    sells = [fill for fill in fills if fill.side is OrderSide.SELL]
    if not buys or not sells or thesis.mode is None or thesis.opened_at is None:
        raise ValueError(f"{thesis.ref} has no closed position to grade")
    quantity, entry = _average(buys)
    _, exit_price = _average(sells)
    return ThesisGrade(
        thesis_id=thesis.thesis_id,
        graded_at=at,
        mode=thesis.mode,
        source=thesis.source,
        conviction=thesis.conviction,
        exit_reason=exit_reason,
        quantity=quantity,
        entry_price=entry,
        exit_price=exit_price,
        reached=exit_reason == TARGET or exit_price >= thesis.target,
        payoff=exit_price / entry - 1,
        stated_payoff=thesis.target / entry - 1,
        days_held=max(0, (at.date() - thesis.opened_at.date()).days),
        horizon_days=max(0, (thesis.horizon - thesis.opened_at.date()).days),
    )


@dataclass(frozen=True, slots=True)
class GroupRecord:
    """The closed theses of one source or conviction level: how many, how many reached their
    target, and their mean payoff beside the mean payoff they stated."""

    group: str
    closed: int
    reached: int
    mean_payoff: Decimal
    mean_stated_payoff: Decimal


def _group(name: str, grades: Sequence[ThesisGrade]) -> GroupRecord:
    count = len(grades)
    return GroupRecord(
        group=name,
        closed=count,
        reached=sum(1 for grade in grades if grade.reached),
        mean_payoff=sum((grade.payoff for grade in grades), ZERO) / count,
        mean_stated_payoff=sum((grade.stated_payoff for grade in grades), ZERO) / count,
    )


def record(
    grades: Iterable[ThesisGrade], segments: Mapping[int, str] | None = None
) -> list[GroupRecord]:
    """The record of the book, then of each source, each segment of the analyst's (one model and
    one prompt hash, from ``segments``: thesis id to segment) and each conviction level that has a
    closed thesis. Nothing is reported for a group without one."""
    every = list(grades)
    if not every:
        return []
    named = segments or {}
    groups: dict[str, list[ThesisGrade]] = {}
    for item in every:
        groups.setdefault(f"source {item.source.value}", []).append(item)
    for item in every:
        if item.source is ThesisSource.ANALYST and item.thesis_id in named:
            groups.setdefault(f"analyst {named[item.thesis_id]}", []).append(item)
    for item in every:
        groups.setdefault(f"conviction {item.conviction.value}", []).append(item)
    if any(item.held_as is Expression.OPTION for item in every):
        # An option's payoff is the return on its premium: the two holdings are also read apart.
        for item in every:
            groups.setdefault(f"held as {item.held_as.value}", []).append(item)
    return [_group("book", every), *(_group(name, found) for name, found in groups.items())]


def opinion_record(
    opinions: Iterable[tuple[SecondOpinion, AnalystCall]], grades: Iterable[ThesisGrade]
) -> list[OpinionScore]:
    """The analyst's second opinions graded against the theses they were on, per segment: the
    mean Brier score of those whose thesis has closed. Nothing is reported for a segment without
    one; an opinion on a thesis that never filled is never graded."""
    reached = {grade.thesis_id: grade.reached for grade in grades}
    scores: dict[str, list[float]] = {}
    for opinion, call in opinions:
        if opinion.thesis_id in reached:
            scores.setdefault(call.label, []).append(
                brier(opinion.probability, reached[opinion.thesis_id])
            )
    return [
        OpinionScore(segment=segment, graded=len(found), brier=sum(found) / len(found))
        for segment, found in scores.items()
    ]


@dataclass(frozen=True, slots=True)
class PairRecord:
    """The closed options of one direction beside their twins, in the base currency: the premium
    paid and what the options made, and what the twins that have closed made at the book's base
    size. A twin still running is counted, never valued."""

    direction: str
    closed: int
    premium: Decimal
    option_pnl: Decimal
    twins_closed: int
    twin_pnl: Decimal

    @property
    def twins_open(self) -> int:
        return self.closed - self.twins_closed


def pair_record(pairs: Iterable[PairGrade]) -> list[PairRecord]:
    """The record of the options beside their twins, calls and puts apart. Nothing is reported
    for a direction without a closed option."""
    found: list[PairRecord] = []
    every = list(pairs)
    for right, name in ((OptionRight.CALL, "calls"), (OptionRight.PUT, "puts")):
        mine = [pair for pair in every if pair.right is right]
        if not mine:
            continue
        twins = [pair.twin_pnl for pair in mine if pair.twin_pnl is not None]
        found.append(
            PairRecord(
                direction=name,
                closed=len(mine),
                premium=sum((pair.premium_base for pair in mine), ZERO),
                option_pnl=sum((pair.option_pnl for pair in mine), ZERO),
                twins_closed=len(twins),
                twin_pnl=sum(twins, ZERO),
            )
        )
    return found
