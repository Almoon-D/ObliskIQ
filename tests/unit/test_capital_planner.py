"""The reservation planner over the fake broker: the cash line used at once, a fresh thesis
spared, a question when only a protected thesis is left, postpone moving the reservation, no
answer by the deadline proceeding, the year-end option only when the date allows, a weak position
sold at once and a strong one held to the last safe days and never past them, a reservation met
on time across a holiday and a daylight-saving change, and every reason the plan stops.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import respx
from capital_rig import capital_parts, make_rebalancer
from fake_broker import FakeBroker
from order_rig import build_rig, terms
from service_rig import EMIM, IBGM, IWDA, SERVICE_VENUES, skeleton_market, three_fund_core

from obliskiq.calendars.service import Calendars
from obliskiq.capital.planner import Category, Pace, ReservationPlan
from obliskiq.capital.strategy import ExpectedReturn, PositionFacts, Protected, Protection
from obliskiq.config import ObliskConfig
from obliskiq.domain.capital import (
    OptionEffect,
    QuestionKind,
    QuestionState,
    ReservationState,
    WithdrawalReservation,
)
from obliskiq.domain.orders import (
    ExecutionMode,
    Fill,
    IntentRole,
    IntentState,
    OrderSide,
    ReconcileFinding,
    ReconcileRun,
    ReconcileStatus,
    Sleeve,
)
from obliskiq.errors import CalendarError, TaxError
from obliskiq.execution.reconcile import CASH_UNEXPLAINED
from obliskiq.orchestration.parts import CapitalParts
from obliskiq.store import capital as capital_repo
from obliskiq.store import orders as repo
from obliskiq.tax.book import TaxHooks

D = Decimal
#: The satellite's stand-in thesis: iShares STOXX Europe 600 Banks on Xetra, in euros.
EXV1 = 89005163
#: Wednesday 21 October 2026, 08:00 UTC: Amsterdam and Xetra trade (10:00 local, summer time).
NOW = datetime(2026, 10, 21, 8, 0, tzinfo=UTC)
PAPER = ExecutionMode.PAPER


@pytest.fixture(scope="module")
def calendars(shipped_settings: ObliskConfig) -> Calendars:
    return Calendars.from_settings(
        shipped_settings, reference=date(2026, 10, 9), venues=SERVICE_VENUES
    )


@pytest.fixture
async def rig(
    make_session: Callable[..., Any],
    shipped_settings: ObliskConfig,
    calendars: Calendars,
    tmp_path: Path,
) -> AsyncIterator[Any]:
    with respx.mock(assert_all_called=False) as router:
        broker = FakeBroker(router)
        built = await build_rig(
            settings=three_fund_core(shipped_settings),
            session=make_session(),
            broker=broker,
            tmp_path=tmp_path,
            calendars=calendars,
        )
        skeleton_market(broker)
        broker.quote(EXV1, bid="19.99", ask="20.01", last="20.00", sigma="20%")
        broker.contracts[EXV1] = ("EXV1", "IBIS", "EUR")
        broker.marks[EXV1] = D(20)
        built.clock.now = NOW
        await built.to_paper()
        yield built
        await built.aclose()


_counter = iter(range(1, 100_000))


async def bought(
    rig: Any, conid: int, units: str, price: str, *, sleeve: Sleeve, at: datetime
) -> None:
    """A paper entry of ``sleeve`` that filled at ``at``: the book paid for it, the broker holds
    it and the registry assigns it."""
    stored = await rig.intent(
        terms(conid=conid, quantity=units, limit=price),
        state=IntentState.SUBMITTED,
        sleeve=sleeve,
        reason="rebalance",
    )
    fill = Fill(
        execution_id=f"bought-{next(_counter)}",
        coid=stored.coid,
        conid=conid,
        side=OrderSide.BUY,
        quantity=D(units),
        price=D(price),
        commission=D(0),
        executed_at=at,
    )
    await rig.store(repo.upsert_fills, [fill])
    await rig.book.advance(stored.intent.intent_id, IntentState.FILLED, "test")
    held = rig.broker.positions.get(conid, (D(0), "EUR"))[0]
    rig.broker.hold(conid, str(held + D(units)), "EUR")
    await rig.assign(conid, sleeve)


async def reserve(
    rig: Any, amount: str, due: date, *, currency: str = "EUR"
) -> WithdrawalReservation:
    return await rig.store(
        capital_repo.record_reservation,
        WithdrawalReservation(
            amount=D(amount),
            currency=currency,
            due_date=due,
            reason="a test withdrawal",
            created_at=rig.clock(),
            created_by="owner",
            updated_at=rig.clock(),
        ),
    )


async def survey(parts: CapitalParts) -> ReservationPlan:
    return await parts.planner.survey(PAPER)


@dataclass
class Expects:
    """A strategy value with an expected return per conid, from a sufficient layer."""

    returns: dict[int, Decimal]
    evidence: Any = "sufficient"

    def would_exit(self, position: PositionFacts, *, today: date) -> bool:
        return False

    def protection(self, position: PositionFacts, *, today: date) -> Protected | None:
        return None

    def expected_return(self, position: PositionFacts, *, today: date) -> ExpectedReturn | None:
        value = self.returns.get(position.conid)
        return None if value is None else ExpectedReturn(value, self.evidence, "a test layer")

    def target_weight(self, position: PositionFacts, *, today: date) -> Decimal | None:
        return None


# ── the cash line and a fresh thesis ──


async def test_the_cash_line_meets_a_reservation_at_once_and_investable_drops(rig: Any) -> None:
    parts = capital_parts(rig)
    await reserve(rig, "300", NOW.date() + timedelta(days=10))
    plan = await survey(parts)
    (line,) = plan.lines
    assert line.from_cash == D(300)
    assert line.sales == []
    assert line.status.startswith("funded")
    assert plan.capital is not None
    assert (plan.capital.reserved, plan.capital.investable) == (D(300), D(700))
    # The rebalancer spends nothing that is reserved, and recomputes its targets on 700.
    rebalance = await make_rebalancer(rig).survey(PAPER)
    assert rebalance.spendable == D(1000) - D(300) - D(25)
    assert rebalance.core_capital == D(665)  # 95 % of 700
    assert "R1" in plan.render()
    assert "from the cash line" in plan.render()


async def test_the_planner_sells_a_core_line_and_spares_a_fresh_thesis(rig: Any) -> None:
    await bought(rig, IBGM, "3", "177.50", sleeve=Sleeve.CORE, at=NOW - timedelta(days=40))
    await bought(rig, EXV1, "20", "20", sleeve=Sleeve.SATELLITE, at=NOW - timedelta(days=2))
    parts = capital_parts(rig)
    await parts.tax.catch_up()
    await reserve(rig, "300", NOW.date() + timedelta(days=14))
    plan = await survey(parts)
    (line,) = plan.lines
    assert line.from_cash == D("67.50")  # 1,000 less 532.50 and 400
    assert [(s.symbol, s.units, s.category) for s in line.sales] == [
        ("IBGM", D(2), Category.ORDINARY)
    ]
    sale = line.sales[0]
    assert sale.tax is not None
    assert sale.trading is not None
    assert sale.pace is Pace.TRANCHES
    assert sale.today_units == D(2)
    assert line.status == "planned"
    actions = await parts.planner.apply(plan)
    (intent,) = actions.intents
    assert (intent.conid, intent.side, intent.terms.quantity) == (IBGM, OrderSide.SELL, D(2))
    assert (intent.role, intent.reason, intent.sleeve) == (
        IntentRole.EXIT,
        "withdrawal",
        Sleeve.CORE,
    )
    # The working sale's proceeds count toward the reservation: nothing more is planned.
    again = await survey(parts)
    assert again.lines[0].sales == []
    assert again.lines[0].status.startswith("funded")


async def test_losses_are_sold_before_gains(rig: Any) -> None:
    await bought(rig, IBGM, "3", "150", sleeve=Sleeve.CORE, at=NOW - timedelta(days=200))  # gain
    await bought(rig, EMIM, "2", "60", sleeve=Sleeve.CORE, at=NOW - timedelta(days=200))  # loss
    parts = capital_parts(rig)
    await parts.tax.catch_up()
    await reserve(rig, "800", NOW.date() + timedelta(days=14))  # 330 of cash: 470 to raise
    plan = await survey(parts)
    loss, gain = plan.lines[0].sales
    assert (loss.symbol, loss.units) == ("EMIM", D(2))  # the loss goes first
    assert loss.tax is not None
    assert loss.tax <= 0
    assert gain.symbol == "IBGM"
    assert gain.tax is not None
    assert gain.tax > 0


# ── asking before cutting ──


async def test_a_question_when_only_a_protected_thesis_is_left_and_postpone_moves_the_date(
    rig: Any,
) -> None:
    await bought(rig, EXV1, "40", "20", sleeve=Sleeve.SATELLITE, at=NOW - timedelta(days=2))
    parts = capital_parts(rig)
    reservation = await reserve(rig, "500", NOW.date() + timedelta(days=10))
    plan = await survey(parts)
    (line,) = plan.lines
    assert line.status.startswith("waiting for the owner's answer")
    assert plan.problems == []
    (question,) = plan.to_ask
    assert question.kind is QuestionKind.PROTECTED_CUT
    assert {o.key for o in question.options} == {"proceed", "postpone", "reduce"}
    assert question.default == "proceed"
    assert "EXV1 (entry window until" in question.context
    reduce = question.option("reduce")
    assert reduce is not None
    assert reduce.amount == D(200)
    postpone = question.option("postpone")
    assert postpone is not None
    assert postpone.due_date is not None
    # The entry window ends 18 days from now; settled two sessions later, plus the buffer.
    assert postpone.due_date > NOW.date() + timedelta(days=18)
    actions = await parts.planner.apply(plan)
    (asked,) = actions.asked
    assert asked.question_id is not None
    assert (await parts.planner.apply(await survey(parts))).asked == []  # asked once
    answered = await parts.questions.answer(asked.question_id, "postpone", actor="owner")
    assert "now due" in answered.effect
    moved = await rig.store(capital_repo.get_reservation, reservation.reservation_id)
    assert moved.due_date == postpone.due_date
    history = await rig.store(capital_repo.reservation_history, reservation.reservation_id)
    assert "postpone" in history[-1].detail
    # On the new date the protection has ended before the last safe day: the thesis is sold
    # after its window, with no new question.
    later = await survey(parts)
    assert later.to_ask == []
    (sale,) = later.lines[0].sales
    assert sale.symbol == "EXV1"
    assert sale.start > NOW.date() + timedelta(days=17)


async def test_without_an_answer_by_the_deadline_the_withdrawal_wins(rig: Any) -> None:
    await bought(rig, EXV1, "40", "20", sleeve=Sleeve.SATELLITE, at=NOW - timedelta(days=2))
    parts = capital_parts(rig)
    await reserve(rig, "500", NOW.date() + timedelta(days=10))
    (question,) = (await parts.planner.apply(await survey(parts))).asked
    assert await parts.questions.expire_due() == []  # not yet
    rig.clock.now = question.deadline + timedelta(minutes=40)  # inside Xetra's order window
    (expired,) = await parts.questions.expire_due()
    assert expired.question.state is QuestionState.EXPIRED
    assert expired.question.answer == "proceed"
    plan = await survey(parts)
    (sale,) = plan.lines[0].sales
    assert (sale.symbol, sale.category, sale.pace) == ("EXV1", Category.PROTECTED, Pace.HOLD)
    assert sale.today_units > 0  # the deadline is the start of its last safe sessions
    (intent,) = (await parts.planner.apply(plan)).intents
    assert intent.sleeve is Sleeve.SATELLITE
    assert intent.reason == "withdrawal"


async def test_reduce_lowers_the_reservation(rig: Any) -> None:
    await bought(rig, EXV1, "40", "20", sleeve=Sleeve.SATELLITE, at=NOW - timedelta(days=2))
    parts = capital_parts(rig)
    reservation = await reserve(rig, "500", NOW.date() + timedelta(days=10))
    (question,) = (await parts.planner.apply(await survey(parts))).asked
    answered = await parts.questions.answer(question.question_id, "reduce", actor="owner")
    assert answered.effect.endswith("200 EUR")
    reduced = await rig.store(capital_repo.get_reservation, reservation.reservation_id)
    assert reduced.amount == D(200)
    assert (await survey(parts)).lines[0].status.startswith("funded")


def long_window(settings: ObliskConfig) -> ObliskConfig:
    """Entry windows of sixty days: a thesis bought in December is protected into February."""
    planner = settings.capital.planner.model_copy(update={"entry_window_days": 60})
    capital = settings.capital.model_copy(update={"planner": planner})
    return settings.model_copy(update={"capital": capital})


async def test_the_year_end_option_is_offered_only_when_the_date_allows(rig: Any) -> None:
    rig.clock.now = datetime(2026, 12, 15, 9, 0, tzinfo=UTC)
    await bought(
        rig, EXV1, "40", "15", sleeve=Sleeve.SATELLITE, at=rig.clock.now - timedelta(days=3)
    )
    parts = capital_parts(rig, long_window(rig.settings))
    await parts.tax.catch_up()  # bought at 15, worth 20: a gain the sale would tax this year
    january = await reserve(rig, "500", date(2027, 1, 20))
    (question,) = (await survey(parts)).to_ask
    year_end = question.option("after_year_end")
    assert year_end is not None
    assert year_end.not_before == date(2027, 1, 4)  # Xetra's first session of 2027
    assert year_end.tax_cost is not None
    assert year_end.tax_cost > 0
    await rig.store(
        capital_repo.change_reservation,
        january.reservation_id,
        at=rig.clock.now,
        actor="owner",
        detail="test",
        due_date=date(2026, 12, 30),
    )
    (december,) = (await survey(parts)).to_ask
    assert december.option("after_year_end") is None  # the date does not allow it
    # Chosen, the sales wait for the new tax year.
    await rig.store(
        capital_repo.change_reservation,
        january.reservation_id,
        at=rig.clock.now,
        actor="owner",
        detail="test",
        due_date=date(2027, 1, 20),
    )
    (asked,) = (await parts.planner.apply(await survey(parts))).asked
    await parts.questions.answer(asked.question_id, "after_year_end", actor="owner")
    (sale,) = (await survey(parts)).lines[0].sales
    assert sale.start >= date(2027, 1, 4)
    assert sale.today_units == 0


async def test_no_year_end_option_without_a_tax_this_year(rig: Any) -> None:
    rig.clock.now = datetime(2026, 12, 15, 9, 0, tzinfo=UTC)
    await bought(
        rig, EXV1, "40", "25", sleeve=Sleeve.SATELLITE, at=rig.clock.now - timedelta(days=3)
    )
    parts = capital_parts(rig, long_window(rig.settings))
    await parts.tax.catch_up()  # bought at 25, worth 20: a loss
    await reserve(rig, "500", date(2027, 1, 20))
    (question,) = (await survey(parts)).to_ask
    assert question.option("after_year_end") is None


# ── pacing ──


async def test_a_weak_position_goes_at_once_and_a_strong_one_waits_for_the_last_safe_days(
    rig: Any,
) -> None:
    await bought(rig, IBGM, "3", "177.50", sleeve=Sleeve.CORE, at=NOW - timedelta(days=90))
    await bought(rig, EMIM, "2", "46.42", sleeve=Sleeve.CORE, at=NOW - timedelta(days=90))
    strategy = Expects({IBGM: D(0), EMIM: D("0.30")})
    parts = capital_parts(rig, strategy=strategy)
    due = date(2026, 11, 13)  # a Friday: settled by Wednesday the 11th
    await reserve(rig, "950", due)  # 374.66 of cash: all of IBGM and one EMIM
    plan = await survey(parts)
    weak, strong = plan.lines[0].sales
    assert (weak.symbol, weak.pace, weak.today_units) == ("IBGM", Pace.NOW, D(3))
    assert "proceeds set aside" in weak.reason
    assert (strong.symbol, strong.units, strong.pace) == ("EMIM", D(1), Pace.HOLD)
    assert strong.last_safe == date(2026, 11, 9)  # T+2 on Amsterdam settles the 11th
    assert strong.start == date(2026, 11, 6)  # the last two safe sessions
    assert strong.today_units == 0
    (later,) = plan.to_ask
    assert later.kind is QuestionKind.LATER_DATE
    assert later.default == "proceed"
    postpone = later.option("postpone")
    assert postpone is not None
    assert postpone.due_date is not None
    assert postpone.due_date > due
    # Its first held session sells half (rounded up); the last safe one all that is left.
    rig.clock.now = datetime(2026, 11, 6, 9, 0, tzinfo=UTC)
    assert (await survey(parts)).lines[0].sales[1].today_units == D(1)
    rig.clock.now = datetime(2026, 11, 9, 9, 0, tzinfo=UTC)
    final = (await survey(parts)).lines[0].sales
    assert final[1].today_units == final[1].units  # never past the last safe day
    assert (await survey(parts)).to_ask  # still unanswered: offered until it is


async def test_an_expected_return_counts_only_when_its_evidence_is_sufficient(rig: Any) -> None:
    await bought(rig, EMIM, "2", "46.42", sleeve=Sleeve.CORE, at=NOW - timedelta(days=90))
    parts = capital_parts(rig, strategy=Expects({EMIM: D("0.30")}, evidence="inconclusive"))
    await reserve(rig, "950", date(2026, 11, 13))
    (sale,) = (await survey(parts)).lines[0].sales
    assert sale.pace is Pace.TRANCHES  # the default ranking: an ordinary holding
    assert sale.strategy == sale.proceeds * rig.settings.capital.planner.strategy_cost.ordinary


async def test_an_overweight_is_sold_at_once(rig: Any) -> None:
    await bought(rig, IBGM, "5", "177.50", sleeve=Sleeve.CORE, at=NOW - timedelta(days=90))
    parts = capital_parts(rig)
    await reserve(rig, "400", NOW.date() + timedelta(days=14))
    # Before the core's first survey it states no target: the line ranks as an ordinary one.
    (sale, *_) = (await survey(parts)).lines[0].sales
    assert sale.category is Category.ORDINARY
    # Its survey notes the target portfolio: 887.50 held against 68 % of 950.
    await make_rebalancer(rig, value=parts.value).survey(PAPER)
    (sale, *_) = (await survey(parts)).lines[0].sales
    assert (sale.category, sale.pace) == (Category.OVERWEIGHT, Pace.NOW)
    assert sale.strategy == 0


# ── settlement across a holiday and a daylight-saving change ──


async def test_a_reservation_is_met_on_time_across_a_holiday(rig: Any) -> None:
    rig.clock.now = datetime(2026, 12, 14, 9, 0, tzinfo=UTC)
    await bought(rig, EMIM, "2", "46.42", sleeve=Sleeve.CORE, at=rig.clock.now - timedelta(days=90))
    parts = capital_parts(rig, strategy=Expects({EMIM: D("0.30")}))
    await reserve(rig, "950", date(2026, 12, 28))  # settled by the 26th, over Christmas
    (sale,) = (await survey(parts)).lines[0].sales
    # The 23rd would settle on the 28th (the 24th, then the 28th): the 22nd is the last safe.
    assert sale.last_safe == date(2026, 12, 22)
    assert sale.start == date(2026, 12, 21)


async def test_a_held_sale_follows_its_venues_clock_across_the_daylight_saving_change(
    rig: Any,
) -> None:
    rig.clock.now = datetime(2026, 10, 23, 7, 40, tzinfo=UTC)  # Friday: 09:40 in Amsterdam
    await bought(rig, EMIM, "2", "46.42", sleeve=Sleeve.CORE, at=rig.clock.now - timedelta(days=90))
    parts = capital_parts(rig, strategy=Expects({EMIM: D("0.30")}))
    await reserve(rig, "1000", date(2026, 10, 30))  # settles by the 28th: Monday is the last day
    plan = await survey(parts)
    (sale,) = plan.lines[0].sales
    assert (sale.start, sale.last_safe) == (date(2026, 10, 23), date(2026, 10, 26))
    (friday,) = (await parts.planner.apply(plan)).intents
    assert friday.terms.quantity == D(1)  # half of two, over the two held sessions
    await rig.book.advance(friday.intent_id, IntentState.EXPIRED, "test")
    # Monday at 07:40 UTC is 08:40 in Amsterdam after the clocks went back: not yet open.
    rig.clock.now = datetime(2026, 10, 26, 7, 40, tzinfo=UTC)
    early = await parts.planner.apply(await survey(parts))
    assert early.intents == []
    assert "not tradable now" in early.waiting[0]
    rig.clock.now = datetime(2026, 10, 26, 8, 40, tzinfo=UTC)
    (monday,) = (await parts.planner.apply(await survey(parts))).intents
    assert monday.terms.quantity == D(2)  # nothing sold on Friday: all of it on the last day


# ── why a plan stops ──


async def test_the_plan_stops_on_the_kill_switch_a_freeze_or_an_unreadable_account(
    rig: Any,
) -> None:
    parts = capital_parts(rig)
    assert (await survey(parts)).lines == []  # no reservation, nothing to plan
    await reserve(rig, "300", NOW.date() + timedelta(days=10))
    await rig.kill.turn_on(actor="test", reason="drill")
    assert "kill switch" in str((await survey(parts)).blocked)
    await rig.kill.turn_off(actor="test", reason="over")
    await rig.store(
        repo.record_reconciliation,
        ReconcileRun(
            at=NOW,
            status=ReconcileStatus.DRIFT,
            findings=(ReconcileFinding(kind="cash", detail="d", freezes=True),),
        ),
        anchor=False,
    )
    assert "reconciliation is drift" in str((await survey(parts)).blocked)
    await rig.store(
        repo.record_reconciliation,
        ReconcileRun(at=NOW, status=ReconcileStatus.GREEN, findings=()),
        anchor=False,
    )
    rig.broker.unavailable.add("ledger")
    assert "the account cannot be read" in str((await survey(parts)).blocked)
    rig.broker.unavailable.clear()
    rig.broker.nav = None
    assert "nothing is managed" in str((await survey(parts)).blocked)
    assert "nothing planned" in (await survey(parts)).render()


async def test_a_reservation_in_a_currency_without_a_rate_stops_the_plan(rig: Any) -> None:
    parts = capital_parts(rig)
    await reserve(rig, "300", NOW.date() + timedelta(days=10), currency="GBP")
    plan = await survey(parts)
    assert plan.blocked is not None
    assert "GBP" in plan.blocked


async def test_a_reservation_nothing_can_meet_in_time_is_a_problem(rig: Any) -> None:
    await bought(rig, IBGM, "5", "177.50", sleeve=Sleeve.CORE, at=NOW - timedelta(days=90))
    parts = capital_parts(rig)
    await reserve(rig, "500", NOW.date() + timedelta(days=1))  # T+2 and the buffer: too late
    plan = await survey(parts)
    (line,) = plan.lines
    assert line.status.startswith("short by")
    assert plan.problems
    assert plan.problems[0].startswith("R1")
    assert any("settles by" in note for note in line.notes)


async def test_a_sale_that_cannot_be_priced_waits(rig: Any) -> None:
    await bought(rig, IBGM, "3", "177.50", sleeve=Sleeve.CORE, at=NOW - timedelta(days=90))
    parts = capital_parts(rig)
    await reserve(rig, "600", NOW.date() + timedelta(days=14))
    plan = await survey(parts)
    rig.broker.unavailable.add("rules")
    actions = await parts.planner.apply(plan)
    assert actions.intents == []
    assert "cannot be priced" in actions.waiting[0]


# ── the rebalancer leaves the reservation's sales to the planner ──


async def test_the_rebalancer_does_not_sell_while_a_reservation_is_uncovered(rig: Any) -> None:
    await bought(rig, IBGM, "5", "177.50", sleeve=Sleeve.CORE, at=NOW - timedelta(days=90))
    rebalancer = make_rebalancer(rig)
    before = {d.role: d for d in (await rebalancer.survey(PAPER)).decisions}
    assert before["euro_govt_intermediate"].action == "sell"  # 887.50 against a target of 646
    await reserve(rig, "400", NOW.date() + timedelta(days=14))
    after = {d.role: d for d in (await rebalancer.survey(PAPER)).decisions}
    assert after["euro_govt_intermediate"].action == "skip"
    assert "reservation planner" in after["euro_govt_intermediate"].reason


async def test_the_rebalancer_never_buys_back_a_loss_inside_the_two_month_window(rig: Any) -> None:
    await bought(rig, IBGM, "3", "200", sleeve=Sleeve.CORE, at=NOW - timedelta(days=90))
    stored = await rig.intent(
        terms(conid=IBGM, side=OrderSide.SELL, quantity="3", limit="177.50"),
        role=IntentRole.EXIT,
        reason="rebalance",
        state=IntentState.SUBMITTED,
    )
    await rig.store(
        repo.upsert_fills,
        [
            Fill(
                execution_id="loss-sale",
                coid=stored.coid,
                conid=IBGM,
                side=OrderSide.SELL,
                quantity=D(3),
                price=D("177.50"),
                commission=D(0),
                executed_at=NOW - timedelta(days=5),
            )
        ],
    )
    await rig.book.advance(stored.intent.intent_id, IntentState.FILLED, "test")
    rig.broker.positions.pop(IBGM)
    parts = capital_parts(rig)
    await parts.tax.catch_up()
    roles = {d.role: d for d in (await make_rebalancer(rig).survey(PAPER)).decisions}
    assert roles["euro_govt_intermediate"].action == "skip"
    assert "two-month rule" in roles["euro_govt_intermediate"].reason
    assert roles["em_equity"].action == "buy"


async def test_reservations_close_when_withdrawn(rig: Any) -> None:
    reservation = await reserve(rig, "300", NOW.date() + timedelta(days=10))
    await rig.store(
        capital_repo.change_reservation,
        reservation.reservation_id,
        at=NOW,
        actor="owner",
        detail="withdrawn",
        state=ReservationState.WITHDRAWN,
    )
    assert (await survey(capital_parts(rig))).lines == []
    assert OptionEffect.PROCEED.value == "proceed"
    assert IWDA  # the dollar line is not part of these plans


# ── what the strategy says, and what cannot be known ──


@dataclass
class Stance:
    """A strategy value that exits ``exits`` anyway and protects ``guarded`` until ``until``."""

    exits: frozenset[int] = frozenset()
    guarded: frozenset[int] = frozenset()
    until: date | None = None

    def would_exit(self, position: PositionFacts, *, today: date) -> bool:
        return position.conid in self.exits

    def protection(self, position: PositionFacts, *, today: date) -> Protected | None:
        if position.conid not in self.guarded:
            return None
        return Protected(Protection.PENDING_CATALYST, self.until)

    def expected_return(self, position: PositionFacts, *, today: date) -> ExpectedReturn | None:
        return None

    def target_weight(self, position: PositionFacts, *, today: date) -> Decimal | None:
        return None


async def test_an_exit_the_strategy_makes_anyway_costs_nothing_and_goes_at_once(rig: Any) -> None:
    await bought(rig, IBGM, "3", "177.50", sleeve=Sleeve.CORE, at=NOW - timedelta(days=90))
    parts = capital_parts(rig, strategy=Stance(exits=frozenset({IBGM})))
    await reserve(rig, "1200", NOW.date() + timedelta(days=14))
    (sale,) = (await survey(parts)).lines[0].sales
    assert (sale.category, sale.pace, sale.strategy) == (Category.EXIT_ANYWAY, Pace.NOW, 0)
    assert sale.today_units == D(3)


async def test_a_middling_expected_return_is_sold_in_tranches(rig: Any) -> None:
    await bought(rig, EMIM, "2", "46.42", sleeve=Sleeve.CORE, at=NOW - timedelta(days=90))
    parts = capital_parts(rig, strategy=Expects({EMIM: D("0.05")}))
    await reserve(rig, "950", date(2026, 11, 13))
    (sale,) = (await survey(parts)).lines[0].sales
    assert sale.pace is Pace.TRANCHES
    assert "expects 5.0% a year: sold in tranches" in sale.reason
    assert sale.strategy == sale.proceeds * D("0.05")  # the expected return is the cost


async def test_a_protection_with_no_end_postpones_by_the_configured_days(rig: Any) -> None:
    await bought(rig, EXV1, "40", "20", sleeve=Sleeve.SATELLITE, at=NOW - timedelta(days=2))
    parts = capital_parts(rig, strategy=Stance(guarded=frozenset({EXV1})))
    due = NOW.date() + timedelta(days=10)
    await reserve(rig, "500", due)
    plan = await survey(parts)
    (question,) = plan.to_ask
    assert "EXV1 (pending catalyst)" in question.context
    postpone = question.option("postpone")
    assert postpone is not None
    assert postpone.due_date == due + timedelta(days=30)  # capital.questions.postpone_days
    first = await parts.planner.apply(plan)
    assert len(first.asked) == 1
    assert (await parts.planner.apply(plan)).asked == []  # the same question is asked once


async def test_a_question_on_the_last_safe_day_is_due_at_its_open(rig: Any) -> None:
    await bought(rig, EXV1, "40", "20", sleeve=Sleeve.SATELLITE, at=NOW - timedelta(days=2))
    parts = capital_parts(rig)
    await reserve(rig, "500", date(2026, 10, 25))  # settled by the 23rd: today is the last day
    (question,) = (await survey(parts)).to_ask
    assert question.deadline == NOW  # the session is open: no later deadline leaves time to sell


async def test_without_readable_lots_or_costs_the_plan_says_unknown(
    rig: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    await bought(rig, IBGM, "3", "177.50", sleeve=Sleeve.CORE, at=NOW - timedelta(days=90))
    parts = capital_parts(rig)
    await reserve(rig, "600", NOW.date() + timedelta(days=14))

    async def unreadable() -> Any:
        raise TaxError("the lots are corrupt")

    def unpriced(*args: Any, **kwargs: Any) -> Any:
        raise ValueError("no schedule")

    monkeypatch.setattr(parts.tax, "hooks", unreadable)
    monkeypatch.setattr(parts.planner._costs, "leg_cost", unpriced)
    plan = await survey(parts)
    (sale,) = plan.lines[0].sales
    assert (sale.tax, sale.trading) == (None, None)
    assert "tax unknown, trading unknown" in plan.render()


async def test_a_tax_the_lots_cannot_estimate_is_unknown(
    rig: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    await bought(rig, IBGM, "3", "177.50", sleeve=Sleeve.CORE, at=NOW - timedelta(days=90))
    parts = capital_parts(rig)
    await parts.tax.catch_up()
    await reserve(rig, "600", NOW.date() + timedelta(days=14))

    def refused(self: Any, *args: Any, **kwargs: Any) -> Any:
        raise TaxError("a security the engine does not know")

    monkeypatch.setattr(TaxHooks, "trial_tax", refused)
    (sale,) = (await survey(parts)).lines[0].sales
    assert sale.tax is None
    assert sale.trading is not None


async def test_a_holding_the_calendars_cannot_place_is_left_out_with_why(
    rig: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    await bought(rig, IBGM, "3", "177.50", sleeve=Sleeve.CORE, at=NOW - timedelta(days=90))
    await bought(rig, EMIM, "2", "46.42", sleeve=Sleeve.CORE, at=NOW - timedelta(days=90))
    parts = capital_parts(rig)
    await reserve(rig, "900", NOW.date() + timedelta(days=14))
    calendars = rig.calendars
    real_venue, real_last = calendars.venue_for_ibkr, calendars.last_safe_session

    def venue_for_ibkr(code: str) -> str:
        if code == "AEB":
            raise CalendarError("no venue for AEB")
        return real_venue(code)

    monkeypatch.setattr(calendars, "venue_for_ibkr", venue_for_ibkr)
    assert (await survey(parts)).lines[0].sales == []  # both list on Amsterdam
    monkeypatch.setattr(calendars, "venue_for_ibkr", real_venue)

    def beyond(venue: str, settle_by: date, *, from_day: date) -> date | None:
        raise CalendarError(f"{venue} is not covered to {settle_by.isoformat()}")

    monkeypatch.setattr(calendars, "last_safe_session", beyond)
    (line,) = (await survey(parts)).lines
    assert line.sales == []
    assert any("is not covered" in note for note in line.notes)
    monkeypatch.setattr(calendars, "last_safe_session", real_last)


async def test_a_position_with_a_working_sale_is_left_to_it(rig: Any) -> None:
    await bought(rig, IBGM, "3", "177.50", sleeve=Sleeve.CORE, at=NOW - timedelta(days=90))
    await bought(rig, EXV1, "10", "20", sleeve=Sleeve.CORE, at=NOW - timedelta(days=90))
    working = await rig.intent(
        terms(conid=IBGM, side=OrderSide.SELL, quantity="2", limit="177.50"),
        role=IntentRole.EXIT,
        reason="rebalance",
        state=IntentState.SUBMITTED,
    )
    await rig.store(
        repo.upsert_fills,
        [
            Fill(
                execution_id="part", coid=working.coid, conid=IBGM, side=OrderSide.SELL,
                quantity=D(1), price=D("177.50"), commission=D(0), executed_at=NOW,
            )
        ],
    )  # fmt: skip
    await rig.intent(  # a withdrawal sale of a position no longer held: nothing to wait for
        terms(conid=EMIM, side=OrderSide.SELL, quantity="2", limit="46"),
        role=IntentRole.EXIT,
        reason="withdrawal",
        state=IntentState.SUBMITTED,
    )
    parts = capital_parts(rig)
    await make_rebalancer(rig, value=parts.value).survey(PAPER)
    await reserve(rig, "600", NOW.date() + timedelta(days=14))
    (line,) = (await survey(parts)).lines
    assert line.from_cash == D(445)  # 1,000 less 532.50 and 200, plus the unit sold at 177.50
    # IBGM is the rebalancer's to sell: its proceeds are not the reservation's, and it is spared.
    assert [s.symbol for s in line.sales] == ["EXV1"]
    (sale,) = line.sales
    # No core role names EXV1: the core sells it anyway.
    assert sale.category is Category.EXIT_ANYWAY


async def test_unexplained_cash_alone_never_stops_a_withdrawal(rig: Any) -> None:
    await bought(rig, IBGM, "3", "177.50", sleeve=Sleeve.CORE, at=NOW - timedelta(days=90))
    parts = capital_parts(rig)
    await reserve(rig, "600", NOW.date() + timedelta(days=14))
    unexplained = ReconcileFinding(kind=CASH_UNEXPLAINED, detail="E1", freezes=True)
    await rig.store(
        repo.record_reconciliation,
        ReconcileRun(at=NOW, status=ReconcileStatus.DRIFT, findings=(unexplained,)),
        anchor=False,
    )
    plan = await survey(parts)
    assert plan.blocked is None  # the positions reconcile: the withdrawal's sales go ahead
    assert plan.lines[0].sales
    (intent,) = (await parts.planner.apply(plan)).intents
    assert intent.side is OrderSide.SELL
    # New buys wait for the classification.
    assert "reconciliation is drift" in str((await make_rebalancer(rig).survey(PAPER)).blocked)
    positions = ReconcileFinding(kind="position_mismatch", detail="d", freezes=True)
    await rig.store(
        repo.record_reconciliation,
        ReconcileRun(at=NOW, status=ReconcileStatus.DRIFT, findings=(unexplained, positions)),
        anchor=False,
    )
    assert "reconciliation is drift" in str((await survey(parts)).blocked)
