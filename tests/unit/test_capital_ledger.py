"""The cash-flow ledger, reservations, owner questions, unexplained cash events and external
holdings in the store, and the services over them: what they record, what they refuse, and the
audit they keep.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import sqlite3
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from obliskiq.capital.flows import CashLedger, LedgerRefusal, parse_record_id, record_label
from obliskiq.capital.questions import DEADLINE_ACTOR, OwnerQuestions, QuestionRefusal
from obliskiq.capital.twr import account_twr
from obliskiq.config import ObliskConfig
from obliskiq.config.schema_instruments import EligibilityConfig
from obliskiq.domain.capital import (
    Adjustment,
    AdjustmentKind,
    CashEvent,
    CashEventState,
    Deposit,
    ExternalHolding,
    OptionEffect,
    OwnerQuestion,
    QuestionKind,
    QuestionOption,
    QuestionState,
    ReservationState,
    Withdrawal,
    WithdrawalReservation,
)
from obliskiq.domain.tax import OpenLossWindow, SecurityIdentity, SecurityKind
from obliskiq.errors import StoreError
from obliskiq.store import Store, open_store
from obliskiq.store import capital as repo
from obliskiq.store import fx as fx_repo
from obliskiq.store import lots as lots_repo
from obliskiq.store import nav as nav_repo
from obliskiq.store.worker import StoreWorker

D = Decimal
AT = datetime(2026, 10, 21, 10, 0, tzinfo=UTC)
TODAY = AT.date()


class Clock:
    def __init__(self) -> None:
        self.now = AT

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
async def worker(shipped_settings: ObliskConfig, tmp_path: Path) -> AsyncIterator[StoreWorker]:
    made = StoreWorker(lambda: open_store(shipped_settings, tmp_path))
    yield made
    await made.aclose()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def ledger(worker: StoreWorker, clock: Clock) -> CashLedger:
    return CashLedger(worker, base_currency="EUR", now=clock)


def question(reservation_id: int, *, deadline: datetime = AT + timedelta(days=3)) -> OwnerQuestion:
    return OwnerQuestion(
        kind=QuestionKind.PROTECTED_CUT,
        reservation_id=reservation_id,
        context="R1 needs a cut",
        options=(
            QuestionOption(
                key="proceed",
                effect=OptionEffect.PROCEED,
                label="sell",
                tax_cost=D(10),
                strategy_cost=D(5),
            ),
            QuestionOption(
                key="postpone",
                effect=OptionEffect.POSTPONE,
                label="later",
                tax_cost=None,
                strategy_cost=D(0),
                due_date=TODAY + timedelta(days=40),
            ),
            QuestionOption(
                key="reduce",
                effect=OptionEffect.REDUCE,
                label="less",
                tax_cost=D(0),
                strategy_cost=D(0),
                amount=D(200),
            ),
            QuestionOption(
                key="after_year_end",
                effect=OptionEffect.AFTER_YEAR_END,
                label="next year",
                tax_cost=D(10),
                strategy_cost=D(5),
                not_before=date(2027, 1, 4),
            ),
        ),
        default="proceed",
        deadline=deadline,
        created_at=AT,
    )


# ── ids ──


def test_record_ids_carry_their_kind() -> None:
    assert parse_record_id("R3") == ("reservation", 3)
    assert parse_record_id(" f12 ") == ("flow", 12)
    assert parse_record_id("E1") == ("event", 1)
    assert record_label("event", 4) == "E4"
    for bad in ("3", "R0", "Rx", "X3", ""):
        with pytest.raises(LedgerRefusal, match="not a record id"):
            parse_record_id(bad)


# ── deposits, reservations, withdrawals ──


async def test_a_deposit_is_recorded_and_refused_when_it_is_not_one(
    ledger: CashLedger, worker: StoreWorker
) -> None:
    deposit = ledger.deposit(D("1237.57"), currency=None, on=None, actor="owner", note="savings")
    assert (deposit.currency, deposit.on, deposit.in_book) == ("EUR", TODAY, True)
    made = await ledger.record_deposit(deposit)
    assert made.flow_id == 1
    assert await worker.run(repo.get_flow, 1) == made
    for amount in (D(0), D(-5), D("Infinity")):
        with pytest.raises(LedgerRefusal, match="positive amount"):
            ledger.deposit(amount, currency=None, on=None, actor="owner", note=None)
    with pytest.raises(LedgerRefusal, match="once it is made"):
        ledger.deposit(D(1), currency=None, on=TODAY + timedelta(days=1), actor="o", note=None)
    with pytest.raises(LedgerRefusal, match="currency code"):
        ledger.deposit(D(1), currency="euros", on=None, actor="o", note=None)


async def test_a_reservation_and_its_withdrawal_close_together(
    ledger: CashLedger, worker: StoreWorker
) -> None:
    reservation = ledger.reservation(
        D(1000), currency="EUR", by=TODAY + timedelta(days=15), reason=None, actor="owner"
    )
    assert reservation.reason == "withdrawal"
    made = await ledger.record_reservation(reservation)
    assert made.reservation_id == 1
    with pytest.raises(LedgerRefusal, match="already past"):
        ledger.reservation(D(1), currency=None, by=TODAY - timedelta(days=1), reason="x", actor="o")
    with pytest.raises(LedgerRefusal, match="positive amount"):
        ledger.reservation(D(0), currency=None, by=TODAY, reason="x", actor="o")
    withdrawal = ledger.withdrawal_for(await ledger.open_reservation(1), actor="owner")
    taken = await ledger.record_withdrawal(withdrawal)
    assert (taken.amount, taken.reservation_id) == (D(1000), 1)
    closed = await worker.run(repo.get_reservation, 1)
    assert closed.state is ReservationState.WITHDRAWN
    history = await worker.run(repo.reservation_history, 1)
    assert [e.detail.split(":")[0] for e in history] == [
        "reserved 1000 EUR by 2026-11-05",
        "withdrawn",
    ]
    with pytest.raises(LedgerRefusal, match="is withdrawn"):
        await ledger.open_reservation(1)
    with pytest.raises(LedgerRefusal, match="no reservation R9"):
        await ledger.open_reservation(9)
    unlinked = withdrawal.model_copy(update={"reservation_id": None})
    with pytest.raises(LedgerRefusal, match="ends a reservation"):
        await ledger.record_withdrawal(unlinked)
    with pytest.raises(LedgerRefusal, match="is withdrawn"):
        await ledger.record_withdrawal(withdrawal)


async def test_cancelling_a_reservation_or_an_unshown_flow(
    ledger: CashLedger, worker: StoreWorker, clock: Clock
) -> None:
    await ledger.record_reservation(
        ledger.reservation(D(10), currency=None, by=TODAY, reason="r", actor="o")
    )
    assert "investable capital is restored" in await ledger.cancel("reservation", 1, actor="o")
    assert (await worker.run(repo.get_reservation, 1)).state is ReservationState.CANCELLED
    deposit = await ledger.record_deposit(
        ledger.deposit(D(5), currency=None, on=None, actor="o", note=None)
    )
    assert await ledger.cancel("flow", deposit.flow_id or 0, actor="o") == "flow F1 cancelled"
    assert not (await worker.run(repo.get_flow, 1)).live
    with pytest.raises(LedgerRefusal, match="cannot be cancelled"):
        await ledger.cancel("flow", 1, actor="o")
    shown = await ledger.record_deposit(
        ledger.deposit(D(5), currency=None, on=None, actor="o", note=None)
    )
    await worker.run(repo.match_flow, shown.flow_id, at=clock.now)
    with pytest.raises(LedgerRefusal, match="shown by the broker"):
        await ledger.cancel("flow", shown.flow_id or 0, actor="o")
    with pytest.raises(StoreError, match="cannot be matched"):
        await worker.run(repo.match_flow, shown.flow_id, at=clock.now)
    with pytest.raises(LedgerRefusal, match="is not cancelled; classify it"):
        await ledger.cancel("event", 1, actor="o")


# ── unexplained cash ──


async def test_an_unexplained_change_is_one_pending_event_per_currency_until_classified(
    ledger: CashLedger, worker: StoreWorker, clock: Clock
) -> None:
    first = await worker.run(repo.note_unexplained, "USD", D(50), at=AT)
    again = await worker.run(repo.note_unexplained, "USD", D(55), at=AT + timedelta(minutes=5))
    assert first.event_id == again.event_id == 1
    assert again.amount == D(55)
    assert again.detected_at == AT
    pending = await ledger.pending_event(1)
    dividend = ledger.classification(
        pending, AdjustmentKind.DIVIDEND, actor="owner", in_book=False, note="ASML"
    )
    assert isinstance(dividend, Adjustment)
    assert dividend.matched_at == AT
    assert (dividend.amount, dividend.in_book, dividend.event_id) == (D(55), False, 1)
    made = await ledger.classify(pending, dividend, actor="owner")
    assert made.flow_id == 1
    (closed,) = await worker.run(repo.cash_events)
    assert closed.state is CashEventState.CLASSIFIED
    assert closed.classified_by == "owner"
    assert await worker.run(repo.cash_events, pending_only=True) == []
    with pytest.raises(LedgerRefusal, match="already classified"):
        await ledger.pending_event(1)
    with pytest.raises(LedgerRefusal, match="no cash event E7"):
        await ledger.pending_event(7)
    with pytest.raises(LedgerRefusal, match="already classified"):
        await ledger.classify(pending, dividend, actor="owner")
    # A new change in the same currency opens a new event.
    assert (await worker.run(repo.note_unexplained, "USD", D(-9), at=AT)).event_id == 2


async def test_an_event_classified_as_a_deposit_or_a_withdrawal_takes_its_sign(
    ledger: CashLedger, worker: StoreWorker
) -> None:
    incoming = await worker.run(repo.note_unexplained, "EUR", D(300), at=AT)
    outgoing = await worker.run(repo.note_unexplained, "USD", D(-40), at=AT)
    deposit = ledger.classification(incoming, "deposit", actor="o", in_book=True, note=None)
    assert isinstance(deposit, Deposit)
    assert deposit.amount == D(300)
    withdrawal = ledger.classification(outgoing, "withdrawal", actor="o", in_book=True, note=None)
    assert isinstance(withdrawal, Withdrawal)
    assert withdrawal.signed == D(-40)
    with pytest.raises(LedgerRefusal, match="money out"):
        ledger.classification(outgoing, "deposit", actor="o", in_book=True, note=None)
    with pytest.raises(LedgerRefusal, match="money in"):
        ledger.classification(incoming, "withdrawal", actor="o", in_book=True, note=None)
    snapshot = await ledger.snapshot()
    assert [e.currency for e in snapshot.events] == ["EUR", "USD"]


# ── owner questions ──


async def _reserved(worker: StoreWorker) -> WithdrawalReservation:
    return await worker.run(
        repo.record_reservation,
        WithdrawalReservation(
            amount=D(500),
            currency="EUR",
            due_date=TODAY + timedelta(days=10),
            reason="a car",
            created_at=AT,
            created_by="owner",
            updated_at=AT,
        ),
    )


async def test_postpone_and_reduce_change_the_reservation_with_an_audit(
    worker: StoreWorker, clock: Clock
) -> None:
    reservation = await _reserved(worker)
    questions = OwnerQuestions(worker, now=clock)
    asked = await questions.ask(question(1), actor="planner")
    assert asked is not None
    assert asked.question_id == 1
    assert await questions.ask(question(1), actor="planner") is None  # one open at a time
    answered = await questions.answer(1, "postpone", actor="owner")
    assert answered.effect == f"R1 is now due {(TODAY + timedelta(days=40)).isoformat()}"
    assert answered.question.state is QuestionState.ANSWERED
    assert answered.question.chosen is not None
    assert answered.question.chosen.key == "postpone"
    with pytest.raises(QuestionRefusal, match="already answered"):
        await questions.answer(1, "reduce", actor="owner")
    second = await questions.ask(question(1), actor="planner")
    assert second is not None
    reduced = await questions.answer(second.question_id or 0, "reduce", actor="owner")
    assert reduced.effect == "R1 is now 200 EUR"
    current = await worker.run(repo.get_reservation, reservation.reservation_id)
    assert (current.amount, current.due_date) == (D(200), TODAY + timedelta(days=40))
    third = await questions.ask(question(1), actor="planner")
    assert third is not None
    later = await questions.answer(third.question_id or 0, "after_year_end", actor="owner")
    assert "wait until 2027-01-04" in later.effect
    fourth = await questions.ask(question(1), actor="planner")
    assert fourth is not None
    with pytest.raises(QuestionRefusal, match="no option 'later'"):
        await questions.answer(fourth.question_id or 0, "later", actor="owner")
    with pytest.raises(QuestionRefusal, match="no question Q99"):
        await questions.answer(99, "proceed", actor="owner")
    assert len(await questions.listed()) == 4
    assert len(await questions.listed(open_only=True)) == 1


async def test_an_unanswered_question_takes_its_default_at_the_deadline(
    worker: StoreWorker, clock: Clock
) -> None:
    await _reserved(worker)
    questions = OwnerQuestions(worker, now=clock)
    await questions.ask(question(1), actor="planner")
    assert await questions.expire_due() == []
    clock.now = AT + timedelta(days=3)
    (expired,) = await questions.expire_due()
    assert expired.question.state is QuestionState.EXPIRED
    assert expired.question.answered_by == DEADLINE_ACTOR
    assert expired.effect == "R1's plan may sell what it needs"
    assert await questions.expire_due() == []


async def test_a_question_on_a_closed_reservation_changes_nothing(
    worker: StoreWorker, clock: Clock
) -> None:
    await _reserved(worker)
    questions = OwnerQuestions(worker, now=clock)
    await questions.ask(question(1), actor="planner")
    await worker.run(
        repo.change_reservation,
        1,
        at=AT,
        actor="o",
        detail="gone",
        state=ReservationState.CANCELLED,
    )
    answered = await questions.answer(1, "postpone", actor="owner")
    assert answered.effect == "R1 is no longer open: nothing to change"


def test_a_question_is_coherent() -> None:
    good = question(1)
    with pytest.raises(ValueError, match="one of the options"):
        good.model_copy(update={"default": "nope"}).model_validate(
            good.model_dump() | {"default": "nope"}
        )
    with pytest.raises(ValueError, match="names exactly what it changes"):
        QuestionOption(
            key="postpone", effect=OptionEffect.POSTPONE, label="x", tax_cost=None,
            strategy_cost=None,
        )  # fmt: skip
    with pytest.raises(ValueError, match="own key"):
        OwnerQuestion.model_validate(
            good.model_dump() | {"options": [good.options[0], good.options[0]]}
        )
    with pytest.raises(ValueError, match="open question has no answer"):
        OwnerQuestion.model_validate(good.model_dump() | {"answer": "proceed"})
    with pytest.raises(ValueError, match="answer is stamped"):
        OwnerQuestion.model_validate(good.model_dump() | {"state": "answered", "answer": "proceed"})


def test_the_store_keeps_questions_and_flows_whole(store: Store) -> None:
    reservation = repo.record_reservation(
        store,
        WithdrawalReservation(
            amount=D(1), currency="EUR", due_date=TODAY, reason="r", created_at=AT,
            created_by="o", updated_at=AT,
        ),
    )  # fmt: skip
    with pytest.raises(StoreError, match="carries no id"):
        repo.record_reservation(store, reservation)
    asked = repo.record_question(store, question(1), actor="planner")
    with pytest.raises(StoreError, match="carries no id"):
        repo.record_question(store, asked, actor="planner")
    with pytest.raises(StoreError, match="answers it or lets it expire"):
        repo.close_question(store, 1, state=QuestionState.OPEN, answer="proceed", at=AT, actor="o")
    with pytest.raises(sqlite3.IntegrityError):
        store.execute("DELETE FROM owner_questions")
    with pytest.raises(sqlite3.IntegrityError):
        store.execute("UPDATE owner_questions SET context = 'x'")
    flow = repo.record_flow(
        store, Deposit(amount=D(1), currency="EUR", on=TODAY, recorded_at=AT, recorded_by="o")
    )
    with pytest.raises(StoreError, match="carries no id"):
        repo.record_flow(store, flow)
    with pytest.raises(sqlite3.IntegrityError):
        store.execute("UPDATE cash_flows SET amount = '2'")
    with pytest.raises(sqlite3.IntegrityError):
        store.execute("DELETE FROM cash_flows")
    with pytest.raises(StoreError, match="no reservation 9"):
        repo.change_reservation(store, 9, at=AT, actor="o", detail="x")
    with pytest.raises(StoreError, match="no question 9"):
        repo.close_question(store, 9, state=QuestionState.EXPIRED, answer="x", at=AT, actor="o")
    with pytest.raises(StoreError, match="no cash event 9"):
        repo.classify_event(store, 9, flow, at=AT, actor="o")
    assert repo.cash_flows(store, as_of=TODAY - timedelta(days=1)) == []
    assert repo.reservations(store, [ReservationState.WITHDRAWN]) == []


# ── external holdings (lite's external_positions) ──


def _holding(**overrides: object) -> ExternalHolding:
    fields: dict[str, object] = {
        "broker": "DEGIRO",
        "symbol": "ASML",
        "quantity": D(3),
        "currency": "EUR",
        "market_value_base": D(2400),
        "last_updated": TODAY,
    }
    fields.update(overrides)
    return ExternalHolding.model_validate(fields)


def test_an_external_holding_updates_in_place_even_without_an_isin(store: Store) -> None:
    first = repo.upsert_external(store, _holding(), at=AT)
    second = repo.upsert_external(store, _holding(quantity=D(4), market_value_base=D(3200)), at=AT)
    assert first.holding_id == second.holding_id
    (only,) = repo.external_holdings(store)
    assert (only.quantity, only.market_value_base) == (D(4), D(3200))
    with_isin = repo.upsert_external(store, _holding(isin="NL0010273215"), at=AT)
    assert with_isin.holding_id != first.holding_id
    assert len(repo.external_holdings(store)) == 2
    assert repo.remove_external(store, first.holding_id or 0)
    assert not repo.remove_external(store, 999)


def test_external_holdings_are_unique_per_broker_and_instrument(store: Store) -> None:
    repo.upsert_external(store, _holding(isin="US0378331005", symbol="AAPL"), at=AT)
    with pytest.raises(sqlite3.IntegrityError):
        store.execute(
            "INSERT INTO external_positions (broker, symbol, isin, quantity, currency, "
            "market_value_base, last_updated, recorded_at) VALUES "
            "('DEGIRO', 'AAPL', 'US0378331005', '1', 'USD', '1', '2026-10-21', "
            "'2026-10-21T10:00:00Z')"
        )


def test_an_external_holding_refuses_a_value_that_is_not_finite() -> None:
    for field in ("quantity", "market_value_base"):
        with pytest.raises(ValueError, match="finite"):
            _holding(**{field: D("Infinity")})


# ── what a record refuses, and rows the store cannot read ──


def test_records_refuse_what_cannot_be_true() -> None:
    with pytest.raises(ValueError, match="moves some money"):
        Adjustment(
            category=AdjustmentKind.FEE, amount=D(0), currency="EUR", on=TODAY, recorded_at=AT,
            recorded_by="o",
        )  # fmt: skip
    with pytest.raises(ValueError, match="due on or after the day it is made"):
        WithdrawalReservation(
            amount=D(1), currency="EUR", due_date=TODAY - timedelta(days=1), reason="r",
            created_at=AT, created_by="o", updated_at=AT,
        )  # fmt: skip
    good = question(1)
    with pytest.raises(ValueError, match="the answer is one of the options"):
        OwnerQuestion.model_validate(
            good.model_dump()
            | {"state": "answered", "answer": "sell", "answered_at": AT, "answered_by": "o"}
        )
    with pytest.raises(ValueError, match="says when and by whom"):
        CashEvent(
            currency="EUR", amount=D(1), detected_at=AT, updated_at=AT,
            state=CashEventState.CLASSIFIED,
        )  # fmt: skip
    with pytest.raises(ValueError, match="only a currency, is keyed"):
        SecurityIdentity(
            key="CCY:USD",
            kind=SecurityKind.SHARE,
            isin=None,
            issuer=None,
            tracked_index=None,
            listed=True,
        )
    with pytest.raises(ValueError, match="closes on or after its sale"):
        OpenLossWindow(
            security="1", sold_on=TODAY, closes_on=TODAY - timedelta(days=1),
            loss_per_unit=D(1), units=D(1),
        )  # fmt: skip


def test_the_skeletons_tax_facts_name_allowlisted_conids_once(
    shipped_settings: ObliskConfig,
) -> None:
    eligibility = shipped_settings.eligibility
    fact = eligibility.skeleton_tax_facts[0]
    with pytest.raises(ValueError, match="tax facts are listed once"):
        EligibilityConfig.model_validate(
            eligibility.model_dump() | {"skeleton_tax_facts": [fact.model_dump()] * 2}
        )
    stranger = fact.model_dump() | {"conid": 424242}
    with pytest.raises(ValueError, match=r"the allowlist does not: \[424242\]"):
        EligibilityConfig.model_validate(
            eligibility.model_dump() | {"skeleton_tax_facts": [stranger]}
        )


def test_the_store_refuses_what_a_record_cannot_be(store: Store) -> None:
    closed = WithdrawalReservation(
        amount=D(1), currency="EUR", due_date=TODAY, reason="r", created_at=AT, created_by="o",
        updated_at=AT, state=ReservationState.CANCELLED,
    )  # fmt: skip
    with pytest.raises(StoreError, match="a reservation starts open"):
        repo.record_reservation(store, closed)
    repo.record_reservation(store, closed.model_copy(update={"state": ReservationState.OPEN}))
    repo.record_question(store, question(1), actor="planner")
    with pytest.raises(StoreError, match="has no option 'sell'"):
        repo.close_question(store, 1, state=QuestionState.ANSWERED, answer="sell", at=AT, actor="o")
    repo.close_question(store, 1, state=QuestionState.ANSWERED, answer="proceed", at=AT, actor="o")
    with pytest.raises(StoreError, match="is already answered"):
        repo.close_question(
            store, 1, state=QuestionState.EXPIRED, answer="proceed", at=AT, actor="o"
        )
    with pytest.raises(StoreError, match="is not a rate"):
        fx_repo.record_rates(store, [fx_repo.FxRate("USD", TODAY, D(0), "test", AT)])


def test_rows_the_store_cannot_read_fail_loudly(store: Store) -> None:
    repo.upsert_external(store, _holding(), at=AT)
    store.execute("UPDATE external_positions SET quantity = 'three'")
    with pytest.raises(StoreError, match=r"external_positions.quantity holds 'three'"):
        repo.external_holdings(store)
    store.execute("UPDATE external_positions SET quantity = 'Infinity'")
    with pytest.raises(StoreError, match="external_positions row cannot be read"):
        repo.external_holdings(store)
    store.execute(
        "INSERT INTO tax_loss_windows (position, security, sold_on, closes_on, loss_per_unit, "
        "units) VALUES (1, '1', '2026-10-01', '2026-10-21', '1', '-1')"
    )
    with pytest.raises(StoreError, match="a tax_loss_windows row is invalid"):
        lots_repo.load_tax_state(store)


async def test_a_question_the_store_refuses_for_another_reason_is_not_swallowed(
    worker: StoreWorker, clock: Clock
) -> None:
    questions = OwnerQuestions(worker, now=clock)
    with pytest.raises(StoreError, match="carries no id"):
        await questions.ask(question(1).model_copy(update={"question_id": 7}), actor="planner")


async def test_the_ledger_refuses_a_withdrawal_or_an_event_it_cannot_place(
    ledger: CashLedger,
) -> None:
    loose = Withdrawal(amount=D(5), currency="EUR", on=TODAY, recorded_at=AT, recorded_by="o")
    with pytest.raises(LedgerRefusal, match="a withdrawal ends a reservation"):
        await ledger.record_withdrawal(loose)
    unsaved = CashEvent(currency="EUR", amount=D(3), detected_at=AT, updated_at=AT)
    flow = ledger.classification(unsaved, AdjustmentKind.FEE, actor="o", in_book=True, note=None)
    with pytest.raises(LedgerRefusal, match="the cash event has no id"):
        await ledger.classify(unsaved, flow, actor="o")


# ── the time-weighted return over the store ──


async def test_the_accounts_return_values_a_foreign_flow_at_its_days_rate(
    shipped_settings: ObliskConfig, worker: StoreWorker
) -> None:
    for day, nav in ((date(2026, 10, 1), "1000"), (date(2026, 10, 2), "1190")):
        await worker.run(nav_repo.record_nav, nav_repo.NavReading(day, D(nav), "EUR", "test", AT))
    dollars = Deposit(
        amount=D(100), currency="USD", on=date(2026, 10, 2), recorded_at=AT, recorded_by="o",
        matched_at=AT,
    )  # fmt: skip
    await worker.run(repo.record_flow, dollars)
    since, until = date(2026, 9, 30), date(2026, 10, 2)
    unknown = await account_twr(worker, shipped_settings, since=since, as_of=until)
    assert unknown.value is None
    assert unknown.reason == "no USD rate was recorded on 2026-10-02"
    await worker.run(
        fx_repo.record_rates, [fx_repo.FxRate("USD", date(2026, 10, 2), D("0.9"), "test", AT)]
    )
    measured = await account_twr(worker, shipped_settings, since=since, as_of=until)
    assert measured.value == D("0.1")  # (1190 - 90) / 1000 - 1
