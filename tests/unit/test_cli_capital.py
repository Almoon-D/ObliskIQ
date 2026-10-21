"""``obliskiq cash``, ``questions``, ``holdings external`` and ``tax report``, run in-process.

Every command that records something echoes what it would do and records nothing without
``--confirm``; every refusal says why and records nothing.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import io
import logging
import os
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from obliskiq.cli.app import EXIT_INVALID, EXIT_OK, run
from obliskiq.config import load_config
from obliskiq.domain.capital import (
    CashEventState,
    OptionEffect,
    OwnerQuestion,
    QuestionKind,
    QuestionOption,
    ReservationState,
)
from obliskiq.domain.orders import (
    ExecutionMode,
    Fill,
    IntentRole,
    LimitPrice,
    OrderSide,
    OrderTerms,
    Sleeve,
    TimeInForce,
)
from obliskiq.domain.tax import LedgerState, OpenLossWindow, Realisation, TaxLot
from obliskiq.execution.intents import new_intent
from obliskiq.store import capital as capital_repo
from obliskiq.store import lots as lots_repo
from obliskiq.store import open_store
from obliskiq.store import orders as orders_repo

D = Decimal
IBGM = "43614383"


@pytest.fixture(autouse=True)
def _restore_root_logging() -> Iterator[None]:
    yield
    logging.getLogger().handlers.clear()


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config_dir: Path) -> Path:
    path = tmp_path / "data"
    path.mkdir()
    monkeypatch.setenv("OBLISK_DATA_DIR", str(path))
    monkeypatch.setenv("OBLISK_CONFIG_DIR", str(config_dir))
    return path


@pytest.fixture
def store(data_dir: Path, config_dir: Path) -> Iterator[Any]:
    with open_store(load_config(config_dir).settings, data_dir) as opened:
        yield opened


def _run(*argv: str) -> tuple[int, str]:
    out, err = io.StringIO(), io.StringIO()
    status = run(list(argv), env_file=None, stdout=out, stderr=err)
    return status, out.getvalue() + err.getvalue()


def _today() -> date:
    return datetime.now(UTC).date()


# ── cash deposit ──


def test_a_deposit_is_echoed_and_recorded_only_with_confirm(data_dir: Path, store: Any) -> None:
    status, out = _run("cash", "deposit", "1_237.57", "--note", "salary")
    assert status == EXIT_INVALID
    assert "deposit of 1237.57 EUR" in out
    assert "at once, in the managed book" in out
    assert "deployed toward the targets at once" in out
    assert "nothing was recorded" in out
    assert capital_repo.cash_flows(store) == []
    status, out = _run("cash", "deposit", "1237.57", "--confirm")
    assert (status, out.splitlines()[-1]) == (EXIT_OK, "recorded F1")
    [flow] = capital_repo.cash_flows(store)
    assert (flow.amount, flow.currency, flow.on) == (D("1237.57"), "EUR", _today())


def test_a_deposit_on_the_whole_account_lands_through_the_broker_in_tranches(
    data_dir: Path, config_dir: Path
) -> None:
    owner = config_dir / "owner.yaml"
    owner.write_text(
        owner.read_text(encoding="utf-8")
        + "capital:\n  mode: whole_account\n  cap_base: null\n  deployment:\n    tranche_days: 5\n",
        encoding="utf-8",
    )
    status, out = _run("cash", "deposit", "500")
    assert status == EXIT_INVALID
    assert "once the broker's cash shows it" in out
    assert "in equal daily tranches over 5 days" in out


@pytest.mark.parametrize(
    ("argv", "why"),
    [
        (("cash", "deposit", "abc"), "is not an amount"),
        (("cash", "deposit", "-5"), "must be a positive amount"),
        (("cash", "deposit", "NaN"), "must be a positive amount"),
        (("cash", "deposit", "5", "--currency", "euro"), "refused:"),
        (("cash", "deposit", "5", "--date", "2999-01-01"), "recorded once it is made"),
    ],
)
def test_a_deposit_that_cannot_be_recorded_is_refused(
    data_dir: Path, store: Any, argv: tuple[str, ...], why: str
) -> None:
    status, out = _run(*argv, "--confirm")
    assert status == EXIT_INVALID
    assert why in out
    assert capital_repo.cash_flows(store) == []


# ── cash reserve, withdrawn, cancel ──


def test_a_reservation_is_echoed_recorded_and_withdrawn(data_dir: Path, store: Any) -> None:
    due = (_today() + timedelta(days=15)).isoformat()
    status, out = _run("cash", "reserve", "1000", "--by", due, "--reason", "car")
    assert status == EXIT_INVALID
    assert f"withdrawal of 1000 EUR by {due} (car)" in out
    assert "settles at least 2 day(s) before" in out
    assert capital_repo.reservations(store) == []
    status, out = _run("cash", "reserve", "1000", "--by", due, "--reason", "car", "--confirm")
    assert (status, out.splitlines()[-1]) == (EXIT_OK, "recorded R1")
    status, out = _run("cash", "withdrawn", "R1")
    assert status == EXIT_INVALID
    assert "R1: 1000 EUR taken out on" in out
    assert capital_repo.get_reservation(store, 1).state is ReservationState.OPEN
    status, out = _run("cash", "withdrawn", "r1", "--confirm")
    assert (status, out.splitlines()[-1]) == (EXIT_OK, "recorded F1; R1 is withdrawn")
    assert capital_repo.get_reservation(store, 1).state is ReservationState.WITHDRAWN
    status, out = _run("cash", "withdrawn", "R1", "--confirm")
    assert status == EXIT_INVALID
    assert "reservation R1 is withdrawn" in out


def test_withdrawn_takes_a_reservation_and_a_past_due_date_is_refused(data_dir: Path) -> None:
    status, out = _run("cash", "withdrawn", "F1", "--confirm")
    assert (status, "give its R<number>" in out) == (EXIT_INVALID, True)
    status, out = _run("cash", "withdrawn", "R7", "--confirm")
    assert (status, "there is no reservation R7" in out) == (EXIT_INVALID, True)
    status, out = _run("cash", "withdrawn", "7", "--confirm")
    assert (status, "is not a record id" in out) == (EXIT_INVALID, True)
    past = (_today() - timedelta(days=1)).isoformat()
    status, out = _run("cash", "reserve", "10", "--by", past, "--confirm")
    assert (status, "is already past" in out) == (EXIT_INVALID, True)


def test_a_reservation_or_an_unshown_flow_is_cancelled_with_confirm(
    data_dir: Path, store: Any
) -> None:
    due = (_today() + timedelta(days=10)).isoformat()
    assert _run("cash", "reserve", "300", "--by", due, "--confirm")[0] == EXIT_OK
    assert _run("cash", "deposit", "50", "--confirm")[0] == EXIT_OK
    status, out = _run("cash", "cancel", "R1")
    assert status == EXIT_INVALID
    assert f"cancel R1: 300 EUR by {due} is no longer needed" in out
    status, out = _run("cash", "cancel", "R1", "--confirm")
    assert status == EXIT_OK
    assert "reservation R1 cancelled: investable capital is restored" in out
    assert capital_repo.get_reservation(store, 1).state is ReservationState.CANCELLED
    status, out = _run("cash", "cancel", "F1")
    assert status == EXIT_INVALID
    assert "cancel F1 deposit 50 EUR on" in out
    assert "(not shown by the broker's cash)" in out
    status, out = _run("cash", "cancel", "F1", "--confirm")
    assert (status, out.splitlines()[-1]) == (EXIT_OK, "flow F1 cancelled")
    status, out = _run("cash", "cancel", "F1", "--confirm")
    assert status == EXIT_INVALID  # a cancelled flow stays cancelled
    status, out = _run("cash", "cancel", "F9", "--confirm")
    assert (status, "there is no flow F9" in out) == (EXIT_INVALID, True)
    status, out = _run("cash", "cancel", "E1", "--confirm")
    assert (status, "classified, not cancelled" in out) == (EXIT_INVALID, True)


# ── cash classify and list ──


def test_an_unexplained_change_is_classified_with_confirm(data_dir: Path, store: Any) -> None:
    at = datetime.now(UTC)
    capital_repo.note_unexplained(store, "EUR", D("12.40"), at=at)
    status, out = _run("cash", "classify", "E1", "dividend", "--outside-book")
    assert status == EXIT_INVALID
    assert "E1: 12.40 EUR is a dividend" in out
    assert "outside the book" in out
    assert "lifts the freeze on new buys" in out
    assert capital_repo.get_cash_event(store, 1).state is CashEventState.PENDING
    status, out = _run("cash", "classify", "E1", "dividend", "--outside-book", "--confirm")
    assert (status, out.splitlines()[-1]) == (EXIT_OK, "recorded F1; E1 is classified")
    assert capital_repo.get_cash_event(store, 1).state is CashEventState.CLASSIFIED
    status, out = _run("cash", "classify", "E1", "dividend", "--confirm")
    assert (status, "already classified" in out) == (EXIT_INVALID, True)


def test_a_classification_that_contradicts_the_change_is_refused(
    data_dir: Path, store: Any
) -> None:
    capital_repo.note_unexplained(store, "EUR", D("-30"), at=datetime.now(UTC))
    status, out = _run("cash", "classify", "E1", "deposit", "--confirm")
    assert (status, "is money out; it is not a deposit" in out) == (EXIT_INVALID, True)
    status, out = _run("cash", "classify", "R1", "fee", "--confirm")
    assert (status, "give its E<number>" in out) == (EXIT_INVALID, True)
    status, out = _run("cash", "classify", "E4", "fee", "--confirm")
    assert (status, "there is no cash event E4" in out) == (EXIT_INVALID, True)
    status, out = _run("cash", "classify", "E1", "withdrawal", "--confirm")
    assert status == EXIT_OK
    assert "a withdrawal; recorded as a new flow withdrawal 30 EUR" in out
    assert "(shown by the broker's cash)" in out


def test_cash_list_shows_reservations_flows_and_events(data_dir: Path, store: Any) -> None:
    status, out = _run("cash", "list")
    assert status == EXIT_OK
    assert "reservations (0 open):" in out
    assert "flows (0):" in out
    assert "unexplained cash (0 pending):" in out
    due = (_today() + timedelta(days=9)).isoformat()
    _run("cash", "reserve", "200", "--by", due, "--reason", "rent", "--confirm")
    _run("cash", "deposit", "75", "--confirm")
    _run("cash", "cancel", "F1", "--confirm")
    capital_repo.note_unexplained(store, "USD", D("3"), at=datetime.now(UTC))
    status, out = _run("cash", "list")
    assert status == EXIT_OK
    assert "reservations (1 open):" in out
    assert f"R1 open      200 EUR by {due} (rent)" in out
    assert "F1 deposit 75 EUR on" in out
    assert "(cancelled)" in out
    assert "unexplained cash (1 pending):" in out
    assert "E1 pending    3 USD since" in out


# ── questions ──


def _question(store: Any, *, deadline: datetime) -> OwnerQuestion:
    reservation = capital_repo.reservations(store)[0]
    return capital_repo.record_question(
        store,
        OwnerQuestion(
            kind=QuestionKind.PROTECTED_CUT,
            reservation_id=reservation.reservation_id,
            context="R1 needs 400 EUR from a protected position",
            options=(
                QuestionOption(
                    key="proceed", effect=OptionEffect.PROCEED, label="sell it",
                    tax_cost=D("12.5"), strategy_cost=D(20),
                ),
                QuestionOption(
                    key="postpone", effect=OptionEffect.POSTPONE, label="later",
                    tax_cost=None, strategy_cost=None,
                    due_date=deadline.date() + timedelta(days=30),
                ),
            ),
            default="proceed",
            deadline=deadline,
            created_at=datetime.now(UTC),
        ),
        actor="planner",
    )  # fmt: skip


def test_questions_are_listed_and_answered_with_confirm(data_dir: Path, store: Any) -> None:
    status, out = _run("questions", "list")
    assert (status, out.strip()) == (EXIT_OK, "0 question(s) open")
    due = _today() + timedelta(days=20)
    _run("cash", "reserve", "400", "--by", due.isoformat(), "--confirm")
    _question(store, deadline=datetime.now(UTC) + timedelta(days=15))
    status, out = _run("questions", "list")
    assert status == EXIT_OK
    assert "1 question(s) open" in out
    assert "Q1 (protected_cut, open until" in out
    assert "proceed (the default): sell it; tax 12.50, strategy 20.00" in out
    assert "postpone: later; tax unknown, strategy unknown" in out
    status, out = _run("questions", "answer", "Q1", "postpone")
    assert status == EXIT_INVALID
    assert "Q1: postpone: later" in out
    assert "nothing was answered" in out
    status, out = _run("questions", "answer", "q1", "postpone", "--confirm")
    assert status == EXIT_OK
    assert "answered:" in out
    assert _run("questions", "list")[1].strip() == "0 question(s) open"
    status, out = _run("questions", "list", "--all")
    assert "1 question(s)\n" in out
    assert "answered: postpone" in out


def test_an_answer_that_names_nothing_is_refused(data_dir: Path, store: Any) -> None:
    status, out = _run("questions", "answer", "x1", "proceed")
    assert (status, "is not a question id" in out) == (EXIT_INVALID, True)
    status, out = _run("questions", "answer", "Q3", "proceed", "--confirm")
    assert (status, "there is no question Q3" in out) == (EXIT_INVALID, True)
    due = _today() + timedelta(days=20)
    _run("cash", "reserve", "400", "--by", due.isoformat(), "--confirm")
    _question(store, deadline=datetime.now(UTC) + timedelta(days=15))
    status, out = _run("questions", "answer", "1", "sell", "--confirm")
    assert status == EXIT_INVALID
    assert "Q1 has no option 'sell' (options: proceed, postpone)" in out


# ── holdings external ──


def test_external_holdings_are_added_updated_listed_and_removed(data_dir: Path) -> None:
    status, out = _run("holdings", "external", "list")
    assert (status, out.splitlines()[0]) == (EXIT_OK, "0 external holding(s), 0.00 EUR in all")
    add = ("holdings", "external", "add", "--broker", "DEGIRO", "--symbol", "VWCE")
    status, out = _run(*add, "--isin", "IE00BK5BQT80", "--quantity", "10", "--currency", "EUR",
                       "--value", "1_100", "--country", "IE")  # fmt: skip
    assert status == EXIT_OK
    assert "external holding 1: DEGIRO VWCE 10 worth 1100 EUR, confirmed" in out
    status, out = _run(*add, "--isin", "IE00BK5BQT80", "--quantity", "12", "--currency", "EUR",
                       "--value", "1320")  # fmt: skip
    assert "external holding 1: DEGIRO VWCE 12 worth 1320" in out  # updated, not appended
    old = (_today() - timedelta(days=90)).isoformat()
    status, _ = _run("holdings", "external", "add", "--broker", "Bank", "--symbol", "FUND",
                     "--quantity", "1", "--currency", "EUR", "--value", "500",
                     "--as-of", old)  # fmt: skip
    assert status == EXIT_OK
    status, out = _run("holdings", "external", "list")
    assert status == EXIT_OK
    assert "2 external holding(s), 1820.00 EUR in all" in out
    assert f"Bank FUND  1 EUR worth 500, confirmed {old} STALE" in out
    assert "UNRELIABLE:" in out
    assert _run("holdings", "external", "remove", "2") == (EXIT_OK, "external holding 2 removed\n")
    assert "UNRELIABLE" not in _run("holdings", "external", "list")[1]
    status, out = _run("holdings", "external", "remove", "2")
    assert (status, "there is no external holding 2" in out) == (EXIT_INVALID, True)


@pytest.mark.parametrize(
    ("extra", "why"),
    [
        (("--quantity", "ten", "--value", "1"), "is not an amount"),
        (("--quantity", "Infinity", "--value", "1"), "finite amounts"),
        (("--quantity", "1", "--value", "1", "--country", "Spain"), "refused:"),
    ],
)
def test_an_external_holding_that_cannot_be_kept_is_refused(
    data_dir: Path, extra: tuple[str, ...], why: str
) -> None:
    status, out = _run("holdings", "external", "add", "--broker", "B", "--symbol", "S",
                       "--currency", "EUR", *extra)  # fmt: skip
    assert (status, why in out) == (EXIT_INVALID, True)
    assert _run("holdings", "external", "list")[1].startswith("0 external holding(s)")


# ── tax report ──


def test_an_empty_years_report_is_its_header_and_a_provisional_summary(data_dir: Path) -> None:
    status, out = _run("tax", "report", "--year", "2026")
    assert status == EXIT_OK
    header, summary = out.splitlines()
    assert header.startswith("section,security,lot_id")
    assert summary.startswith("summary,")
    assert "provisional: income 0, gains 0" in summary
    assert "not tax advice" in summary


def _fill(store: Any, execution_id: str, *, at: datetime) -> None:
    settings = load_config(Path(os.environ["OBLISK_CONFIG_DIR"])).settings
    intent = new_intent(
        settings,
        sleeve=Sleeve.CORE,
        role=IntentRole.EXIT,
        terms=OrderTerms(
            conid=int(IBGM), side=OrderSide.SELL, quantity=2, price=LimitPrice(limit=165),
            tif=TimeInForce.DAY, venue="SMART",
        ),
        reason="rebalance",
        mode=ExecutionMode.PAPER,
        approval_required=False,
        created_at=at,
        expires_at=at + timedelta(hours=8),
    )  # fmt: skip
    orders_repo.insert_intent(store, intent, coid=str(intent.intent_id), at=at)
    orders_repo.upsert_fills(
        store,
        [
            Fill(
                execution_id=execution_id, coid=str(intent.intent_id), conid=int(IBGM),
                side=OrderSide.SELL, quantity=D(2), price=D(165), commission=D(1),
                executed_at=at, simulated=False,
            )
        ],
    )  # fmt: skip


def test_the_report_lists_realisations_deferred_losses_and_is_written_to_a_file(
    data_dir: Path, store: Any, tmp_path: Path
) -> None:
    at = datetime(2026, 6, 1, tzinfo=UTC)
    lot = TaxLot(
        lot_id="L2", security=IBGM, acquired_on=date(2026, 5, 20), quantity=D(2), cost=D(340),
        deferred_loss=D(8), replacement_units=D(2),
    )  # fmt: skip
    window = OpenLossWindow(
        security=IBGM, sold_on=date(2026, 5, 30), closes_on=date(2026, 7, 30),
        loss_per_unit=D(3), units=D(1),
    )  # fmt: skip
    sold = Realisation(
        security=IBGM, lot_id="L1", acquired_on=date(2026, 1, 5), disposed_on=date(2026, 5, 18),
        quantity=D(2), proceeds=D(330), cost=D(338), released=D(0), deferred=D(8),
    )  # fmt: skip
    _fill(store, "x1", at=at)
    lots_repo.record_taken_fill(
        store,
        lots_repo.TakenFill(execution_id="x1", outcome="applied", detail="sold", taken_at=at),
        state=lots_repo.TaxState(
            lots=(lot,), windows=(window,), ledger=LedgerState(years=(), carried=(), settled=()),
            sequence=2,
        ),
        realisations=[sold],
    )  # fmt: skip
    status, out = _run("tax", "report", "--year", "2026")
    assert status == EXIT_OK
    rows = out.splitlines()
    assert rows[1].startswith("realised,IE00B1FZS806 (43614383),L1,2026-01-05,2026-05-18,2,338,330")
    assert rows[2].startswith("deferred_on_lot,IE00B1FZS806 (43614383),L2")
    assert rows[3].startswith("deferred_pending,IE00B1FZS806 (43614383),,,2026-05-30,1")
    assert "bought by 2026-07-30" in rows[3]
    assert len(_run("tax", "report", "--year", "2025")[1].splitlines()) == 4  # no realisation
    target = tmp_path / "tax-2026.csv"
    status, out = _run("tax", "report", "--year", "2026", "--out", str(target))
    assert status == EXIT_OK
    assert f"the 2026 report is in {target} (not tax advice)" in out
    assert target.read_text(encoding="utf-8").splitlines()[1].startswith("realised,")


def test_a_report_over_lots_no_tax_facts_describe_cannot_be_made(
    data_dir: Path, store: Any
) -> None:
    stray = TaxLot(
        lot_id="L1", security="999999", acquired_on=date(2026, 1, 2), quantity=D(1), cost=D(1),
        deferred_loss=D(0), replacement_units=D(0),
    )  # fmt: skip
    lots_repo.save_tax_lots(store, [stray], saved_at=datetime.now(UTC))
    status, out = _run("tax", "report", "--year", "2026")
    assert status == EXIT_INVALID
    assert "the report cannot be made" in out
    assert "no tax facts describe" in out
