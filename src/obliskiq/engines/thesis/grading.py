"""Grading a closed thesis against what it wrote, and the record per source and conviction
(MASTER_PLAN §5.5).

A grade says whether the target was reached, the payoff realised against the one stated, and the
days held against the horizon. It describes the calls and moves no capital: the book's capital
follows the pre-registered forward record (``research/preregistrations/thesis-book.md``), which
Session 18's scoreboard computes from the same fills.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from ...domain.orders import Fill, OrderSide
from ...domain.thesis import Thesis, ThesisGrade
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


def record(grades: Iterable[ThesisGrade]) -> list[GroupRecord]:
    """The record of the book, then of each source and each conviction level that has a closed
    thesis. Nothing is reported for a group without one."""
    every = list(grades)
    if not every:
        return []
    groups: dict[str, list[ThesisGrade]] = {}
    for item in every:
        groups.setdefault(f"source {item.source.value}", []).append(item)
    for item in every:
        groups.setdefault(f"conviction {item.conviction.value}", []).append(item)
    return [_group("book", every), *(_group(name, found) for name, found in groups.items())]
