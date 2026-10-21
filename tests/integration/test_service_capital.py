"""The service's capital work: the cycle's reserve step asks before cutting a protected position,
applies the default of a question nobody answered and alerts a reservation nothing can meet; the
heartbeat's tax book takes the day's fills, waits for the day's rate and says what it cannot
account for.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import asyncio
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from itertools import count
from typing import Any

import pytest
from order_rig import terms
from service_rig import EMIM, IWDA, ServiceRig

from obliskiq.domain.capital import (
    OptionEffect,
    OwnerQuestion,
    QuestionKind,
    QuestionOption,
    QuestionState,
    WithdrawalReservation,
)
from obliskiq.domain.orders import (
    ExecutionMode,
    Fill,
    IntentRole,
    IntentState,
    OrderSide,
    Sleeve,
)
from obliskiq.orchestration.health import read_heartbeat
from obliskiq.store import capital as capital_repo
from obliskiq.store import lots as lots_repo
from obliskiq.store import orders as repo
from obliskiq.store import runs as run_repo

D = Decimal
#: The satellite's stand-in thesis: iShares STOXX Europe 600 Banks on Xetra, in euros.
EXV1 = 89005163
MONDAY = date(2026, 10, 26)
BEFORE_EUROPE = datetime(2026, 10, 26, 8, 55, tzinfo=UTC)
EUROPE_CYCLE = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)
_ids = count(1)


async def run_until(s: ServiceRig, start: datetime, until: datetime) -> None:
    s.clock.wall.now = start
    await s.service.run(asyncio.Event(), until=until)


def step_of(run: Any, name: str) -> dict[str, Any]:
    return next(step for step in run.detail["steps"] if step["step"] == name)


async def europe_run(s: ServiceRig) -> Any:
    (record,) = await s.rig.store(run_repo.runs_for, command="cycle:europe", session_date=MONDAY)
    return record


async def filled(
    s: ServiceRig, conid: int, units: str, price: str, *, side: OrderSide, at: datetime,
    sleeve: Sleeve = Sleeve.CORE,
) -> None:  # fmt: skip
    """A paper order of ``sleeve`` that filled at ``at``."""
    stored = await s.rig.intent(
        terms(conid=conid, quantity=units, limit=price, side=side),
        state=IntentState.SUBMITTED,
        sleeve=sleeve,
        role=IntentRole.ENTRY if side is OrderSide.BUY else IntentRole.EXIT,
        reason="rebalance",
    )
    fill = Fill(
        execution_id=f"svc-{next(_ids)}", coid=stored.coid, conid=conid, side=side,
        quantity=D(units), price=D(price), commission=D(0), executed_at=at,
    )  # fmt: skip
    await s.rig.store(repo.upsert_fills, [fill])
    await s.rig.book.advance(stored.intent.intent_id, IntentState.FILLED, "test")


async def reserve(s: ServiceRig, amount: str, due: date) -> WithdrawalReservation:
    return await s.rig.store(
        capital_repo.record_reservation,
        WithdrawalReservation(
            amount=D(amount), currency="EUR", due_date=due, reason="a test withdrawal",
            created_at=BEFORE_EUROPE - timedelta(days=1), created_by="owner",
            updated_at=BEFORE_EUROPE - timedelta(days=1),
        ),
    )  # fmt: skip


def satellite_market(s: ServiceRig) -> None:
    s.broker.quote(EXV1, bid="19.99", ask="20.01", last="20.00", sigma="20%")
    s.broker.contracts[EXV1] = ("EXV1", "IBIS", "EUR")
    s.broker.marks[EXV1] = D(20)


# ── the reserve step ──


async def test_a_protected_cut_is_asked_and_alerted_before_anything_is_sold(make: Any) -> None:
    s = await make(paper=True)
    satellite_market(s)
    await filled(s, EXV1, "40", "20", side=OrderSide.BUY, at=BEFORE_EUROPE - timedelta(days=2),
                 sleeve=Sleeve.SATELLITE)  # fmt: skip
    s.broker.hold(EXV1, "40", "EUR")
    await s.rig.assign(EXV1, Sleeve.SATELLITE)
    await reserve(s, "500", MONDAY + timedelta(days=10))
    await run_until(s, BEFORE_EUROPE, EUROPE_CYCLE + timedelta(minutes=1))
    (alert,) = s.notifier.alerts("capital.question")
    assert alert.startswith("🚨 capital.question: Q1: ")
    assert "proceed: " in alert
    assert "(the default)" in alert
    assert "`obliskiq questions answer 1 <option> --confirm`" in alert
    reserve_step = step_of(await europe_run(s), "reserve")
    assert reserve_step["status"] == "ok"
    assert reserve_step["questions"] == 1
    assert reserve_step["intents"] == 0  # nothing protected is sold before the answer
    made = await s.rig.store(repo.intents_in_states, list(IntentState))
    assert all(i.intent.reason != "withdrawal" for i in made)


async def test_a_question_past_its_deadline_takes_its_default_and_says_so(make: Any) -> None:
    s = await make(paper=True)
    reservation = await reserve(s, "100", MONDAY + timedelta(days=10))
    await s.rig.store(
        capital_repo.record_question,
        OwnerQuestion(
            kind=QuestionKind.PROTECTED_CUT, reservation_id=reservation.reservation_id,
            context="R1 needs a cut",
            options=(
                QuestionOption(key="proceed", effect=OptionEffect.PROCEED, label="sell",
                               tax_cost=None, strategy_cost=None),
                QuestionOption(key="postpone", effect=OptionEffect.POSTPONE, label="later",
                               tax_cost=None, strategy_cost=None,
                               due_date=MONDAY + timedelta(days=40)),
            ),
            default="proceed",
            deadline=BEFORE_EUROPE - timedelta(hours=1),
            created_at=BEFORE_EUROPE - timedelta(days=1),
        ),
        actor="planner",
    )  # fmt: skip
    await run_until(s, BEFORE_EUROPE, EUROPE_CYCLE + timedelta(minutes=1))
    (alert,) = s.notifier.alerts("capital.question_expired")
    assert "Q1 had no answer by its deadline; its default 'proceed' applies" in alert
    question = await s.rig.store(capital_repo.get_question, 1)
    assert (question.state, question.answer) == (QuestionState.EXPIRED, "proceed")


async def test_a_reservation_nothing_can_meet_degrades_the_step_and_is_alerted(make: Any) -> None:
    s = await make(paper=True)
    await reserve(s, "5000", MONDAY + timedelta(days=1))  # more than the book, and too soon
    await run_until(s, BEFORE_EUROPE, EUROPE_CYCLE + timedelta(minutes=1))
    (alert,) = s.notifier.alerts("capital.reservation_short")
    assert alert.startswith("🚨 capital.reservation_short: R1")
    reserve_step = step_of(await europe_run(s), "reserve")
    assert reserve_step["status"] == "degraded"
    assert reserve_step["reason"].startswith("R1")


async def test_a_dry_run_plans_the_reservation_and_asks_nothing(make: Any) -> None:
    s = await make(paper=True)
    await reserve(s, "300", MONDAY + timedelta(days=10))
    s.clock.wall.now = EUROPE_CYCLE
    result = await s.service.jobs.cycle("europe", MONDAY, dry_run=True)
    assert result.reservations is not None
    (line,) = result.reservations.lines
    assert line.from_cash == D(300)
    assert result.planner is not None
    assert result.planner.intents == []
    assert s.notifier.alerts("capital.question") == []


# ── the heartbeat's tax book ──


async def test_the_heartbeat_takes_the_fills_and_alerts_what_it_cannot_account_for(
    make: Any,
) -> None:
    s = await make(paper=True)
    at = EUROPE_CYCLE
    await filled(s, EMIM, "2", "46", side=OrderSide.SELL, at=at)  # units the bot never bought
    await filled(s, IWDA, "1", "133", side=OrderSide.BUY, at=at)  # USD: no rate yet today
    usd = s.broker.cash.pop("USD")  # the ledger has no USD line today, so no USD rate
    s.clock.wall.now = at + timedelta(minutes=5)
    await s.service.jobs.heartbeat()
    (incomplete,) = s.notifier.alerts("tax.incomplete")
    assert "a fill the tax lots cannot account for" in incomplete
    assert "exceeds the 0 the lots hold" in incomplete
    (waiting,) = s.notifier.alerts("tax.waiting")
    assert "no USD rate recorded yet today" in waiting
    taken = await s.rig.store(lots_repo.taken_fills)
    assert [t.outcome for t in taken.values()] == ["incomplete"]
    # The ledger shows a USD rate: the next heartbeat records it and takes the purchase.
    s.broker.cash["USD"] = usd
    s.clock.wall.now = at + timedelta(minutes=10)
    await s.service.jobs.heartbeat()
    taken = await s.rig.store(lots_repo.taken_fills)
    assert sorted(t.outcome for t in taken.values()) == ["applied", "incomplete"]
    state = await s.rig.store(lots_repo.load_tax_state)
    assert [(lot.security, lot.cost) for lot in state.lots] == [(str(IWDA), D("114.38"))]


async def test_a_tax_book_that_fails_degrades_the_heartbeat_and_nothing_else(
    make: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = await make(paper=True)

    async def broken() -> Any:
        raise RuntimeError("the lots broke")

    monkeypatch.setattr(s.service.jobs._capital.tax, "catch_up", broken)
    s.clock.wall.now = EUROPE_CYCLE
    await s.service.jobs.heartbeat()
    _, facts = read_heartbeat(s.rig.data_dir / "heartbeat.json")
    assert facts["status"] == "degraded"
    assert facts["problems"] == ["the tax book failed: RuntimeError: the lots broke"]
    assert s.service.jobs.last_heartbeat == EUROPE_CYCLE


async def test_shadow_fills_never_reach_the_tax_lots(make: Any) -> None:
    s = await make()
    await run_until(s, BEFORE_EUROPE, EUROPE_CYCLE + timedelta(minutes=30))
    filled_intents = await s.rig.store(repo.intents_in_states, [IntentState.FILLED])
    assert filled_intents
    assert all(i.intent.mode is ExecutionMode.SHADOW for i in filled_intents)
    assert await s.rig.store(lots_repo.taken_fills) == {}
