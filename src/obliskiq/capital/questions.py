"""Owner questions: the decisions the bot needs from the owner before it cuts what it should not
(MASTER_PLAN §5.15).

The reservation planner asks; the owner answers with ``obliskiq questions answer <id> <option>``
(Telegram and MCP in Session 19, natural language in Session 16). An answer takes effect at once:
*postpone* moves the reservation's due date, *reduce* lowers its amount, *proceed* and *after
year-end* tell the planner what it may sell and when. A question nobody answers by its deadline
expires and its default applies — the option under which the withdrawal is met on time: the
withdrawal wins. Every question, answer and expiry is audited (``question_events``), and so is the
reservation change an answer makes (``reservation_events``).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from ..domain.capital import (
    OptionEffect,
    OwnerQuestion,
    QuestionOption,
    QuestionState,
    ReservationState,
)
from ..errors import ObliskError, StoreError
from ..store import capital as repo
from ..store.db import Store
from ..store.worker import StoreWorker
from ..util.timeutil import utc_now

#: Who the audit names when a deadline passes.
DEADLINE_ACTOR = "deadline (the default applies)"


class QuestionRefusal(ObliskError):
    """An answer that cannot be recorded, and why."""


@dataclass(frozen=True, slots=True)
class Answered:
    """A closed question and what its answer did."""

    question: OwnerQuestion
    effect: str


def _apply(
    store: Store, question: OwnerQuestion, option: QuestionOption, *, at: datetime, actor: str
) -> str:
    """Carry out ``option`` on the question's reservation; what was done."""
    reservation = repo.get_reservation(store, question.reservation_id)
    label = f"R{question.reservation_id}"
    if reservation is None or reservation.state is not ReservationState.OPEN:
        return f"{label} is no longer open: nothing to change"
    detail = f"Q{question.question_id} {option.key}: {option.label}"
    if option.effect is OptionEffect.POSTPONE and option.due_date is not None:
        repo.change_reservation(
            store, question.reservation_id, at=at, actor=actor, detail=detail,
            due_date=option.due_date,
        )  # fmt: skip
        return f"{label} is now due {option.due_date.isoformat()}"
    if option.effect is OptionEffect.REDUCE and option.amount is not None:
        repo.change_reservation(
            store, question.reservation_id, at=at, actor=actor, detail=detail,
            amount=option.amount,
        )  # fmt: skip
        return f"{label} is now {option.amount} {reservation.currency}"
    if option.effect is OptionEffect.AFTER_YEAR_END and option.not_before is not None:
        return f"{label}'s sales wait until {option.not_before.isoformat()}"
    return f"{label}'s plan may sell what it needs"


class OwnerQuestions:
    """Asks, answers and expires owner questions."""

    def __init__(self, worker: StoreWorker, *, now: Callable[[], datetime] = utc_now) -> None:
        self._worker = worker
        self._now = now

    async def listed(self, *, open_only: bool = False) -> list[OwnerQuestion]:
        states = [QuestionState.OPEN] if open_only else None
        return await self._worker.run(lambda store: repo.questions(store, states=states))

    async def ask(self, question: OwnerQuestion, *, actor: str) -> OwnerQuestion | None:
        """Record ``question``; ``None`` when one of its kind is open for the reservation."""
        try:
            return await self._worker.run(repo.record_question, question, actor=actor)
        except StoreError:
            open_ones = await self._worker.run(
                lambda store: repo.questions(
                    store, states=[QuestionState.OPEN], reservation_id=question.reservation_id
                )
            )
            if any(q.kind is question.kind for q in open_ones):
                return None
            raise

    async def answer(self, question_id: int, key: str, *, actor: str) -> Answered:
        """The owner's answer: recorded and carried out together."""
        at = self._now()

        def write(store: Store) -> Answered:
            with store.transaction():
                question = repo.get_question(store, question_id)
                if question is None:
                    raise QuestionRefusal(f"there is no question Q{question_id}")
                if question.state is not QuestionState.OPEN:
                    raise QuestionRefusal(
                        f"Q{question_id} is already {question.state.value} ({question.answer})"
                    )
                option = question.option(key)
                if option is None:
                    keys = ", ".join(o.key for o in question.options)
                    raise QuestionRefusal(f"Q{question_id} has no option {key!r} (options: {keys})")
                effect = _apply(store, question, option, at=at, actor=actor)
                closed = repo.close_question(
                    store, question_id, state=QuestionState.ANSWERED, answer=key, at=at,
                    actor=actor,
                )  # fmt: skip
                return Answered(closed, effect)

        return await self._worker.run(write)

    async def expire_due(self) -> list[Answered]:
        """Apply the default of every open question whose deadline has passed."""
        at = self._now()

        def write(store: Store) -> list[Answered]:
            done: list[Answered] = []
            with store.transaction():
                for question in repo.questions(store, states=[QuestionState.OPEN]):
                    if question.deadline > at or question.question_id is None:
                        continue
                    option = question.option(question.default)
                    if option is None:  # the model refuses a default that is no option
                        continue
                    effect = _apply(store, question, option, at=at, actor=DEADLINE_ACTOR)
                    closed = repo.close_question(
                        store, question.question_id, state=QuestionState.EXPIRED,
                        answer=question.default, at=at, actor=DEADLINE_ACTOR,
                    )  # fmt: skip
                    done.append(Answered(closed, effect))
            return done

        return await self._worker.run(write)
