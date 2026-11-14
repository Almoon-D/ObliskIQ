"""The book's breakers as a measure (``decision/breakers.py``), their store and the stand-down
that needs no store: what a fall beyond the equity reference is, what never raises a peak, what
is ``n/a`` and what is unreadable, and that a trip outlives everything that may be failing.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import sqlite3
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from obliskiq.capital.twr import ExternalFlow
from obliskiq.config import ObliskConfig
from obliskiq.decision.breakers import (
    NOT_MEASURED,
    OK,
    TRIPPED_STATE,
    UNREADABLE,
    BreakerReport,
    book_flows,
    book_of,
    judge,
    release,
    stored_report,
)
from obliskiq.domain.capital import Deposit, Withdrawal
from obliskiq.domain.orders import ExecutionMode
from obliskiq.errors import ExecutionError, ModeChangeRefusedError, StoreError
from obliskiq.execution import modes as modes_module
from obliskiq.execution.killswitch import KillSwitch, file_flag_on
from obliskiq.execution.modes import FORCED_SHADOW_FILE, Modes, forced_shadow_on
from obliskiq.store import breakers as repo
from obliskiq.store import capital as capital_repo
from obliskiq.store import open_database
from obliskiq.store import orders as orders_repo
from obliskiq.store.breakers import BreakerEvent, ManagedReading
from obliskiq.store.db import Store
from obliskiq.store.worker import StoreWorker

REFERENCE = 163606916
DAY = date(2026, 10, 26)
NOON = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)


def reading(
    day: date,
    managed: str,
    reference: str | None = "100",
    *,
    close: bool = True,
    book: Any = "broker",
    conid: int = REFERENCE,
) -> ManagedReading:
    return ManagedReading(
        book=book,
        as_of=day,
        managed=Decimal(managed),
        reference_conid=None if reference is None else conid,
        reference_unit=None if reference is None else Decimal(reference),
        is_close=close,
        recorded_at=datetime(day.year, day.month, day.day, 19, 0, tzinfo=UTC),
    )


def closes(*values: str | tuple[str, str | None], last: date = DAY) -> list[ManagedReading]:
    """Daily closes ending the day before ``last``; a pair is ``(managed, reference)``."""
    first = last - timedelta(days=len(values))
    return [
        reading(first + timedelta(days=i), *(value if isinstance(value, tuple) else (value,)))
        for i, value in enumerate(values)
    ]


def judged(
    settings: ObliskConfig,
    history: list[ManagedReading],
    now: ManagedReading | str,
    *,
    flows: list[ExternalFlow] | str = (),  # type: ignore[assignment]
    at: datetime = NOON,
    **kw: Any,
) -> BreakerReport:
    return judge(
        settings.portfolio_circuit_breaker,
        book="broker",
        at=at,
        closes=history,
        current=now,
        flows=flows,
        timing="in_reading",
        **kw,
    )


def live(managed: str, reference: str | None = "100", day: date = DAY) -> ManagedReading:
    return reading(day, managed, reference, close=False)


# ── the measure ──


def test_the_shipped_thresholds_are_lites_freeze_and_a_deeper_hard_one(
    shipped_settings: ObliskConfig,
) -> None:
    config = shipped_settings.portfolio_circuit_breaker
    assert config.monthly_drawdown_pct == Decimal("0.15")  # lite's
    assert (config.window_days, config.min_observations) == (30, 2)  # lite's
    assert config.action == "freeze_new_buys_and_audit"  # lite's
    assert config.hard_drawdown_pct > config.monthly_drawdown_pct


def test_below_the_minimum_observations_a_breaker_is_not_measured_and_refuses_nothing(
    shipped_settings: ObliskConfig,
) -> None:
    none = judged(shipped_settings, [], live("50"))
    one = judged(shipped_settings, closes("100"), live("50"))
    for report in (none, one):
        assert (report.rolling.status, report.hard.status) == (NOT_MEASURED, NOT_MEASURED)
        assert report.buys_refused is None  # untested, never a freeze and never "fine"
        assert "n/a" in report.rolling.line()
    assert "1 daily close(s)" in one.rolling.reason


def test_a_fall_inside_the_limits_is_ok(shipped_settings: ObliskConfig) -> None:
    report = judged(shipped_settings, closes("100", "104", "102"), live("92"))
    assert (report.rolling.status, report.hard.status) == (OK, OK)
    assert report.rolling.worst is not None
    assert report.rolling.worst.fall == 1 - Decimal(92) / Decimal(104)
    assert report.buys_refused is None


def test_a_fall_of_the_books_own_beyond_the_freeze_trips_the_rolling_month(
    shipped_settings: ObliskConfig,
) -> None:
    report = judged(shipped_settings, closes("100", "100"), live("84"))  # 16 %, reference flat
    assert report.rolling.status == TRIPPED_STATE
    assert report.hard.status == OK
    assert not report.newly_tripped
    refused = report.buys_refused
    assert refused is not None
    assert "rolling-month breaker is tripped" in refused
    assert "freeze_new_buys_and_audit" in refused
    assert "TRIPPED" in report.rolling.line()


def test_a_fall_beyond_the_hard_limit_trips_the_hard_breaker(
    shipped_settings: ObliskConfig,
) -> None:
    report = judged(shipped_settings, closes("100", "100"), live("74"))
    assert (report.rolling.status, report.hard.status) == (TRIPPED_STATE, TRIPPED_STATE)
    assert report.newly_tripped
    assert "hard breaker is tripped" in str(report.buys_refused)


def test_exactly_the_limit_does_not_trip(shipped_settings: ObliskConfig) -> None:
    report = judged(shipped_settings, closes("100", "100"), live("85"))
    assert report.rolling.status == OK


def test_a_markets_fall_is_excused_so_2008_and_2020_trip_nothing(
    shipped_settings: ObliskConfig,
) -> None:
    # The book fell 34 % in a month and global equity 33 %: one point beyond it.
    report = judged(shipped_settings, closes(("100", "100"), ("100", "100")), live("66", "67"))
    assert (report.rolling.status, report.hard.status) == (OK, OK)
    worst = report.rolling.worst
    assert worst is not None
    assert (worst.fall, worst.excused, worst.beyond) == (
        Decimal("0.34"),
        Decimal("0.33"),
        Decimal("0.01"),
    )
    assert "the equity reference fell 33.0%" in report.rolling.reason


def test_a_reference_that_rose_excuses_nothing_and_adds_nothing(
    shipped_settings: ObliskConfig,
) -> None:
    report = judged(shipped_settings, closes(("100", "100"), ("100", "100")), live("90", "120"))
    worst = report.rolling.worst
    assert worst is not None
    assert (worst.fall, worst.excused) == (Decimal("0.1"), Decimal(0))
    assert report.rolling.status == OK


@pytest.mark.parametrize(
    ("then", "now", "other"),
    [(None, "67", REFERENCE), ("100", None, REFERENCE), ("100", "67", REFERENCE + 1)],
)
def test_a_reference_with_no_reading_excuses_nothing_so_the_breaker_trips_on_the_book_alone(
    shipped_settings: ObliskConfig, then: str | None, now: str | None, other: int
) -> None:
    history = [
        reading(DAY - timedelta(days=2), "100", then, conid=other),
        reading(DAY - timedelta(days=1), "100", then, conid=other),
    ]
    report = judged(shipped_settings, history, live("66", now))
    assert (report.rolling.status, report.hard.status) == (TRIPPED_STATE, TRIPPED_STATE)
    assert "none of it is excused" in report.hard.reason


def test_a_missing_reference_does_not_matter_while_the_book_is_inside_its_limits(
    shipped_settings: ObliskConfig,
) -> None:
    report = judged(shipped_settings, closes(("100", None), ("100", None)), live("90", None))
    assert (report.rolling.status, report.hard.status) == (OK, OK)


def test_the_fall_is_measured_from_every_close_not_only_the_highest(
    shipped_settings: ObliskConfig,
) -> None:
    # From the peak (100) the reference excuses the fall; from the later close (98) the
    # reference has risen since, and the book's 18 % from there is its own.
    history = closes(("100", "100"), ("98", "70"))
    report = judged(shipped_settings, history, live("80", "75"))
    worst = report.rolling.worst
    assert worst is not None
    assert worst.since == history[1].as_of
    assert report.rolling.status == TRIPPED_STATE


def test_the_rolling_month_forgets_closes_older_than_its_window_and_the_hard_does_not(
    shipped_settings: ObliskConfig,
) -> None:
    window = shipped_settings.portfolio_circuit_breaker.window_days
    old = reading(DAY - timedelta(days=window + 1), "100")
    recent = closes("72", "72")
    report = judged(shipped_settings, [old, *recent], live("72"))
    assert report.rolling.status == OK  # 72 against a month of 72
    assert report.rolling.closes == 2
    assert report.hard.status == TRIPPED_STATE  # 28 % below the close of five weeks ago
    assert report.hard.closes == 3


def test_an_intraday_value_is_measured_against_the_closes_and_never_raises_the_peak(
    shipped_settings: ObliskConfig,
) -> None:
    # A reading inside the day at 130 is no close: handed in among the closes it is ignored.
    spike = reading(DAY - timedelta(days=1), "130", close=False)
    history = [*closes("100", "100", last=DAY - timedelta(days=1)), spike]
    report = judged(shipped_settings, history, live("90"))
    worst = report.rolling.worst
    assert worst is not None
    assert worst.fall == Decimal("0.1")  # against 100, not against the 130 read inside a day
    assert report.rolling.status == OK
    assert report.rolling.closes == 2


# ── flows ──


def test_a_withdrawal_is_not_a_fall_and_a_deposit_hides_none(
    shipped_settings: ObliskConfig,
) -> None:
    history = closes("1000", "1000")
    out = [ExternalFlow(DAY, Decimal(-300))]
    withdrawn = judged(shipped_settings, history, live("700"), flows=out)
    assert (withdrawn.rolling.status, withdrawn.hard.status) == (OK, OK)
    assert withdrawn.rolling.worst is not None
    assert withdrawn.rolling.worst.fall == 0
    paid_in = [ExternalFlow(DAY, Decimal(300))]
    hidden = judged(shipped_settings, history, live("1000"), flows=paid_in)  # 700 of its own
    assert hidden.hard.status == TRIPPED_STATE
    assert hidden.hard.worst is not None
    assert hidden.hard.worst.fall == Decimal("0.3")


def test_flows_that_cannot_be_valued_or_chained_make_the_breakers_unreadable(
    shipped_settings: ObliskConfig,
) -> None:
    history = closes("1000", "1000")
    unvalued = judged(shipped_settings, history, live("1000"), flows="no USD rate was recorded")
    assert unvalued.rolling.status == UNREADABLE
    assert "no USD rate" in str(unvalued.buys_refused)
    # A deposit larger than the whole book on the day: the period has no positive value left.
    swamped = judged(
        shipped_settings, history, live("1000"), flows=[ExternalFlow(DAY, Decimal(1000))]
    )
    assert swamped.hard.status == UNREADABLE
    assert swamped.buys_refused is not None
    # A withdrawal that takes the book's whole value before a reading: no base to measure from.
    emptied = judge(
        shipped_settings.portfolio_circuit_breaker,
        book="broker",
        at=NOON,
        closes=history,
        current=live("10"),
        flows=[ExternalFlow(history[-1].as_of, Decimal(-1000))],
        timing="after_reading",
    )
    assert emptied.rolling.status == UNREADABLE
    assert "cannot be chained" in emptied.rolling.reason


# ── missing and stale ──


def test_a_missing_nav_makes_the_breakers_unreadable_and_new_buys_are_refused(
    shipped_settings: ObliskConfig,
) -> None:
    report = judged(shipped_settings, closes("100", "100"), "the managed NAV cannot be read: x")
    assert (report.rolling.status, report.hard.status) == (UNREADABLE, UNREADABLE)
    assert "cannot be read" in str(report.buys_refused)
    assert not report.newly_tripped  # unreadable refuses buys; it never forces the mode


def test_a_stale_close_makes_the_breakers_unreadable_and_new_buys_are_refused(
    shipped_settings: ObliskConfig,
) -> None:
    limit = shipped_settings.portfolio_circuit_breaker.max_close_age_days
    last = DAY - timedelta(days=limit)
    fresh = judged(
        shipped_settings, closes("100", "100", last=last + timedelta(days=1)), live("99")
    )
    assert fresh.rolling.status == OK  # exactly the limit old: a long weekend passes
    stale = judged(shipped_settings, closes("100", "100", last=last), live("99"))
    assert (stale.rolling.status, stale.hard.status) == (UNREADABLE, UNREADABLE)
    assert "stale" in str(stale.buys_refused)


# ── the latch ──


def test_a_latched_trip_is_the_hard_breakers_state_whatever_is_measured_now(
    shipped_settings: ObliskConfig,
) -> None:
    latch = BreakerEvent(NOON - timedelta(days=3), "broker", "tripped", "breaker", "fell 30 %")
    for now in (live("100"), "the managed NAV cannot be read: x"):
        report = judged(shipped_settings, closes("100", "100"), now, latched=latch)
        assert report.hard.status == TRIPPED_STATE
        assert not report.newly_tripped  # it is latched already: the mode is not forced twice
        assert "obliskiq breakers reset" in str(report.buys_refused)


def test_after_a_reset_the_hard_breaker_measures_from_the_closes_that_follow(
    shipped_settings: ObliskConfig,
) -> None:
    history = closes("100", "100", "70", "70")
    reset = BreakerEvent(
        history[1].recorded_at + timedelta(hours=1), "broker", "reset", "owner", "looked"
    )
    report = judged(shipped_settings, history, live("70"), reset=reset)
    assert report.hard.status == OK  # 70 against the two closes of 70 since the reset
    assert report.hard.closes == 2
    assert report.rolling.status == TRIPPED_STATE  # the month still holds the closes of 100
    young = judged(shipped_settings, history[:3], live("70", day=history[2].as_of), reset=reset)
    assert young.hard.status == NOT_MEASURED
    assert "since the last reset" in young.hard.reason


# ── the store ──


def test_a_reading_inside_the_day_never_replaces_a_close(store: Store) -> None:
    morning = reading(DAY, "100", close=False)
    assert repo.record_reading(store, morning)
    later = reading(DAY, "101", close=False)
    assert repo.record_reading(store, later)  # the day's later reading replaces the earlier
    close = reading(DAY, "102")
    assert repo.record_reading(store, close)
    assert not repo.record_reading(store, reading(DAY, "150", close=False))  # after the close
    assert repo.record_reading(store, reading(DAY, "103"))  # a close corrects a close
    (kept,) = repo.readings(store, "broker", as_of=DAY)
    assert (kept.managed, kept.is_close) == (Decimal(103), True)
    assert repo.readings(store, "shadow", as_of=DAY) == []  # two books, two series
    assert repo.readings(store, "broker", as_of=DAY - timedelta(days=1)) == []  # point in time


def test_closes_only_leaves_out_the_readings_inside_a_day(store: Store) -> None:
    repo.record_reading(store, reading(DAY - timedelta(days=1), "100"))
    repo.record_reading(store, reading(DAY, "130", close=False))
    assert [r.managed for r in repo.readings(store, "broker", as_of=DAY)] == [100, 130]
    assert [r.managed for r in repo.readings(store, "broker", as_of=DAY, closes_only=True)] == [100]


def test_a_reading_that_is_no_value_or_half_a_reference_is_refused(store: Store) -> None:
    with pytest.raises(StoreError):
        repo.record_reading(store, reading(DAY, "0"))
    with pytest.raises(StoreError):
        repo.record_reading(store, reading(DAY, "NaN"))
    half = ManagedReading("broker", DAY, Decimal(100), REFERENCE, None, True, NOON)
    with pytest.raises(StoreError):
        repo.record_reading(store, half)
    with pytest.raises(sqlite3.IntegrityError):
        store.execute(
            "INSERT INTO managed_nav (book, as_of, managed, is_close, recorded_at) "
            "VALUES ('other', '2026-10-26', '1', 1, '2026-10-26T19:00:00Z')"
        )


def test_the_latch_is_append_only_and_its_latest_row_decides(store: Store) -> None:
    assert repo.tripped(store, "broker") is None
    assert repo.latest_event(store, "broker") is None
    trip = BreakerEvent(NOON, "broker", "tripped", "breaker", "fell 30 %")
    repo.record_event(store, trip)
    assert repo.tripped(store, "broker") == trip
    assert repo.tripped(store, "shadow") is None
    assert repo.latches(store) == [trip]
    for statement in ("UPDATE breaker_events SET detail = 'x'", "DELETE FROM breaker_events"):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            store.execute(statement)
    assert release(store, at=NOON, actor="owner", reason="looked") == ["broker"]
    assert repo.tripped(store, "broker") is None
    reset = repo.latest_reset(store, "broker")
    assert reset is not None
    assert (reset.actor, reset.detail) == ("owner", "looked")
    assert release(store, at=NOON, actor="owner", reason="again") == []  # nothing to release


def test_the_stored_report_reads_the_last_reading_and_names_a_trip_on_the_other_book(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    empty = stored_report(store, shipped_settings, "shadow", at=NOON)
    assert (empty.rolling.status, empty.hard.status) == (NOT_MEASURED, NOT_MEASURED)
    assert "no managed NAV reading is recorded" in empty.rolling.reason
    missing = stored_report(store, shipped_settings, "shadow", at=NOON, live="no NAV")
    assert missing.rolling.status == UNREADABLE  # a reading that failed now is not "n/a"
    for day, value in ((DAY - timedelta(days=2), "100"), (DAY - timedelta(days=1), "100")):
        repo.record_reading(store, reading(day, value, book="shadow"))
    repo.record_reading(store, reading(DAY, "95", close=False, book="shadow"))
    repo.record_event(store, BreakerEvent(NOON, "broker", "tripped", "breaker", "fell 30 %"))
    report = stored_report(store, shipped_settings, "shadow", at=NOON)
    assert (report.rolling.status, report.hard.status, report.live) == (OK, OK, False)
    lines = "\n".join(report.lines())
    assert "hard breaker of the broker book: TRIPPED" in lines
    assert "measured on the reading of 2026-10-26T19:00:00+00:00" in lines
    assert report.buys_refused is None  # the shadow book's own breakers are clear


def test_the_books_flows_are_the_capped_books_or_the_accounts(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    def flow(kind: Any, amount: str, **kw: Any) -> None:
        capital_repo.record_flow(
            store,
            kind(
                amount=Decimal(amount),
                currency=kw.pop("currency", "EUR"),
                on=kw.pop("on", DAY),
                recorded_at=NOON,
                recorded_by="test",
                **kw,
            ),
        )

    flow(Deposit, "500")
    flow(Withdrawal, "200", in_book=False)
    flow(Deposit, "50", on=DAY + timedelta(days=1))  # after as_of: not seen yet
    assert shipped_settings.capital.mode == "cap"
    assert book_flows(store, shipped_settings, as_of=DAY) == [ExternalFlow(DAY, Decimal(500))]
    capital = shipped_settings.capital.model_copy(update={"mode": "whole_account"})
    whole = shipped_settings.model_copy(update={"capital": capital})
    assert book_flows(store, whole, as_of=DAY) == []  # neither has reached the account's cash
    capital_repo.match_flow(store, 2, at=NOON)
    assert book_flows(store, whole, as_of=DAY) == [ExternalFlow(DAY, Decimal(-200))]
    flow(Deposit, "100", currency="USD")
    assert "no USD rate was recorded on 2026-10-26" in str(
        book_flows(store, shipped_settings, as_of=DAY)
    )


def test_a_mode_runs_its_own_book() -> None:
    assert book_of(ExecutionMode.SHADOW) == "shadow"
    assert book_of(ExecutionMode.PAPER) == "broker"


# ── the stand-down and the kill switch with the store unreadable ──


@pytest.fixture
async def worker(tmp_path: Path) -> AsyncIterator[StoreWorker]:
    made = StoreWorker(
        lambda: open_database(tmp_path / "modes.db", busy_timeout_ms=1000, synchronous="NORMAL")
    )
    yield made
    await made.aclose()


class BrokenStore:
    """A store worker whose every read and write fails."""

    async def run(self, *_args: Any, **_kwargs: Any) -> Any:
        raise sqlite3.OperationalError("disk I/O error")


async def test_a_stand_down_is_an_audited_downgrade_while_the_store_works(
    worker: StoreWorker, shipped_settings: ObliskConfig, tmp_path: Path
) -> None:
    modes = Modes(worker, shipped_settings, profile="paper", data_dir=tmp_path)
    await modes.upgrade(ExecutionMode.PAPER, actor="owner", reason="start")
    stood = await modes.stand_down(actor="breaker", reason="the hard breaker tripped")
    assert stood.change is not None
    assert not stood.by_file
    assert (stood.change.from_mode, stood.change.to_mode) == (
        ExecutionMode.PAPER,
        ExecutionMode.SHADOW,
    )
    assert await modes.current() is ExecutionMode.SHADOW
    assert not forced_shadow_on(tmp_path)
    again = await modes.stand_down(actor="breaker", reason="again")
    assert (again.change, again.by_file) == (None, False)  # already shadow


async def test_a_mode_downgrade_works_with_the_store_unreadable(
    worker: StoreWorker, shipped_settings: ObliskConfig, tmp_path: Path
) -> None:
    healthy = Modes(worker, shipped_settings, profile="paper", data_dir=tmp_path)
    await healthy.upgrade(ExecutionMode.PAPER, actor="owner", reason="start")
    broken = Modes(BrokenStore(), shipped_settings, profile="paper", data_dir=tmp_path)  # type: ignore[arg-type]
    with pytest.raises(sqlite3.OperationalError):
        await broken.current()
    stood = await broken.stand_down(actor="breaker", reason="the hard breaker tripped")
    assert (stood.change, stood.by_file) == (None, True)
    # Shadow now, without the store, for this process and for the next one.
    assert await broken.current() is ExecutionMode.SHADOW
    assert await healthy.current() is ExecutionMode.SHADOW
    restarted = Modes(worker, shipped_settings, profile="paper", data_dir=tmp_path)
    assert await restarted.current() is ExecutionMode.SHADOW
    # The owner's next upgrade writes the audit row the store could not take, and lifts the file.
    await restarted.upgrade(ExecutionMode.PAPER, actor="owner", reason="the disk was replaced")
    assert not forced_shadow_on(tmp_path)
    assert await restarted.current() is ExecutionMode.PAPER
    rows = await worker.run(
        lambda store: store.execute(
            "SELECT to_mode, actor, reason FROM mode_audit ORDER BY change_id"
        ).fetchall()
    )
    assert [row["to_mode"] for row in rows] == ["paper", "shadow", "paper"]
    assert "recorded late" in rows[1]["reason"]
    assert "the hard breaker tripped" in rows[1]["reason"]


async def test_a_stand_down_neither_the_store_nor_the_file_can_take_raises(
    shipped_settings: ObliskConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with_no_directory = Modes(BrokenStore(), shipped_settings, profile="paper")  # type: ignore[arg-type]
    with pytest.raises(sqlite3.OperationalError):
        await with_no_directory.stand_down(actor="breaker", reason="x")

    def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise PermissionError("read-only file system")

    monkeypatch.setattr(modes_module, "_set_forced_shadow", refuse)
    modes = Modes(BrokenStore(), shipped_settings, profile="paper", data_dir=tmp_path)  # type: ignore[arg-type]
    with pytest.raises(ExecutionError, match="both failed"):
        await modes.stand_down(actor="breaker", reason="x")


def test_a_forced_shadow_file_that_cannot_be_checked_counts_as_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert not forced_shadow_on(tmp_path)

    def denied(self: Path) -> Any:
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "lstat", denied)
    assert forced_shadow_on(tmp_path)


async def test_an_unreadable_forced_shadow_file_is_still_settled_by_an_upgrade(
    worker: StoreWorker, shipped_settings: ObliskConfig, tmp_path: Path
) -> None:
    modes = Modes(worker, shipped_settings, profile="paper", data_dir=tmp_path)
    await modes.upgrade(ExecutionMode.PAPER, actor="owner", reason="start")
    (tmp_path / FORCED_SHADOW_FILE).write_text("not json", encoding="utf-8")
    assert await modes.current() is ExecutionMode.SHADOW
    await modes.upgrade(ExecutionMode.PAPER, actor="owner", reason="back")
    latest = await worker.run(orders_repo.latest_mode_change)
    assert latest is not None
    assert latest.to_mode is ExecutionMode.PAPER
    assert not forced_shadow_on(tmp_path)


async def test_a_forced_shadow_file_over_a_shadow_store_needs_no_late_row(
    worker: StoreWorker, shipped_settings: ObliskConfig, tmp_path: Path
) -> None:
    modes = Modes(worker, shipped_settings, profile="paper", data_dir=tmp_path)
    (tmp_path / FORCED_SHADOW_FILE).write_text("{}", encoding="utf-8")
    await modes.upgrade(ExecutionMode.PAPER, actor="owner", reason="start")
    rows = await worker.run(lambda store: store.execute("SELECT * FROM mode_audit").fetchall())
    assert len(rows) == 1


async def test_an_upgrade_is_refused_while_the_forced_shadow_file_cannot_be_removed(
    worker: StoreWorker,
    shipped_settings: ObliskConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modes = Modes(worker, shipped_settings, profile="paper", data_dir=tmp_path)
    (tmp_path / FORCED_SHADOW_FILE).write_text("{}", encoding="utf-8")
    monkeypatch.setattr(Path, "unlink", lambda *_args, **_kwargs: None)
    with pytest.raises(ModeChangeRefusedError, match="cannot be removed"):
        await modes.upgrade(ExecutionMode.PAPER, actor="owner", reason="back")
    assert await modes.current() is ExecutionMode.SHADOW


async def test_an_upgrade_is_refused_while_the_broker_books_hard_breaker_is_latched(
    worker: StoreWorker, shipped_settings: ObliskConfig, tmp_path: Path
) -> None:
    modes = Modes(worker, shipped_settings, profile="paper", data_dir=tmp_path)
    await worker.run(
        repo.record_event, BreakerEvent(NOON, "broker", "tripped", "breaker", "fell 30 %")
    )
    with pytest.raises(ModeChangeRefusedError, match="obliskiq breakers reset"):
        await modes.upgrade(ExecutionMode.PAPER, actor="owner", reason="back")
    assert await modes.current() is ExecutionMode.SHADOW
    await worker.run(lambda store: release(store, at=NOON, actor="owner", reason="looked"))
    await modes.upgrade(ExecutionMode.PAPER, actor="owner", reason="back")
    assert await modes.current() is ExecutionMode.PAPER


async def test_the_kill_switch_works_with_the_store_unreadable(
    shipped_settings: ObliskConfig, tmp_path: Path
) -> None:
    kill = KillSwitch(tmp_path, BrokenStore(), shipped_settings)  # type: ignore[arg-type]
    problems = await kill.turn_on(actor="owner", reason="everything is failing")
    assert len(problems) == 1  # the database flag could not be written; the file flag was
    assert "database flag" in problems[0]
    assert file_flag_on(tmp_path)
    assert await kill.is_on()


# ── configuration ──


@pytest.mark.parametrize(
    ("changes", "match"),
    [
        ({"hard_drawdown_pct": Decimal("0.15")}, "hard_drawdown_pct sits beyond"),
        ({"close_cycle": "midnight"}, "close_cycle: midnight is not a cycle"),
        ({"equity_reference_conid": 1}, "equity_reference_conid: 1 is not pinned"),
        ({"min_observations": 1}, "min_observations"),
        ({"monthly_drawdown_pct": Decimal(0)}, "monthly_drawdown_pct"),
    ],
)
def test_the_breakers_configuration_refuses_what_cannot_work(
    shipped_settings: ObliskConfig, changes: dict[str, Any], match: str
) -> None:
    block = {**shipped_settings.portfolio_circuit_breaker.model_dump(), **changes}
    payload = {**shipped_settings.model_dump(), "portfolio_circuit_breaker": block}
    with pytest.raises(ValidationError, match=match):
        ObliskConfig.model_validate(payload)
