"""The freeze in the core's rebalancer and the breakers' reading over the capital reader: no buy
while a breaker is tripped or unreadable, sales as they were, no sale a breaker made, and what a
cycle's reading stores.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import dataclasses
import sqlite3
from collections.abc import AsyncIterator, Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import respx
from capital_rig import capital_parts, make_rebalancer
from fake_broker import FakeBroker
from order_rig import build_rig
from service_rig import EMIM, IBGM, IWDA, SERVICE_VENUES, skeleton_market, three_fund_core

from obliskiq.calendars.service import Calendars
from obliskiq.config import ObliskConfig
from obliskiq.decision import breakers as breakers_module
from obliskiq.decision.breakers import NOT_MEASURED, OK, TRIPPED_STATE, UNREADABLE, Breakers
from obliskiq.decision.rebalance_core import RebalancePlan
from obliskiq.domain.capital import Adjustment, AdjustmentKind
from obliskiq.domain.orders import ExecutionMode, HoldingAssignment, Sleeve
from obliskiq.store import breakers as repo
from obliskiq.store import capital as capital_repo
from obliskiq.store import orders as orders_repo
from obliskiq.store.breakers import ManagedReading

#: Monday 26 October 2026, 10:00 in Amsterdam and London's morning: both venues trade.
OPEN = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)
TODAY = OPEN.date()
#: One IWDA in euros at the skeleton market's quotes: the equity reference of these tests.
IWDA_EUR = Decimal("126.0330")
UNIT_EUR = {IBGM: Decimal("177.50"), EMIM: Decimal("46.42"), IWDA: Decimal("126.033")}


def referenced(settings: ObliskConfig) -> ObliskConfig:
    """``settings`` whose equity reference is IWDA, which the fake broker quotes."""
    breaker = settings.portfolio_circuit_breaker.model_copy(update={"equity_reference_conid": IWDA})
    return three_fund_core(settings).model_copy(update={"portfolio_circuit_breaker": breaker})


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
            settings=referenced(shipped_settings),
            session=make_session(),
            broker=broker,
            tmp_path=tmp_path,
            calendars=calendars,
        )
        skeleton_market(broker)
        built.clock.now = OPEN
        await built.reconciled()
        yield built
        await built.aclose()


async def closes(rig: Any, *values: str, book: Any = "shadow", last: date = TODAY) -> None:
    """Daily closes of the book ending the day before ``last``, the reference flat beside them."""
    first = last - timedelta(days=len(values))
    for i, value in enumerate(values):
        day = first + timedelta(days=i)
        await rig.store(
            repo.record_reading,
            ManagedReading(
                book=book,
                as_of=day,
                managed=Decimal(value),
                reference_conid=IWDA,
                reference_unit=IWDA_EUR,
                is_close=True,
                recorded_at=datetime(day.year, day.month, day.day, 19, 0, tzinfo=UTC),
            ),
        )


async def hold(rig: Any, conid: int, units: str, currency: str) -> None:
    """The core holds ``units`` the book paid for (its cash falls by their value)."""
    rig.broker.hold(conid, units, currency)
    await rig.store(
        orders_repo.assign_holding,
        HoldingAssignment(
            conid=conid, owner=Sleeve.CORE, assigned_by="test", assigned_at=OPEN, reason="test"
        ),
    )
    await rig.store(
        capital_repo.record_flow,
        Adjustment(
            category=AdjustmentKind.CORRECTION,
            amount=-(Decimal(units) * UNIT_EUR[conid]),
            currency="EUR",
            on=TODAY,
            recorded_at=OPEN,
            recorded_by="test",
        ),
    )


def sells(plan: RebalancePlan) -> list[tuple[int | None, Decimal]]:
    return [(d.conid, d.quantity) for d in plan.decisions if d.action == "sell"]


def buys(plan: RebalancePlan) -> list[int | None]:
    return [d.conid for d in plan.decisions if d.action == "buy"]


# ── the freeze in the rebalancer ──


async def test_a_book_with_no_close_yet_buys_and_says_its_breakers_are_not_measured(
    rig: Any,
) -> None:
    plan = await make_rebalancer(rig).survey(ExecutionMode.SHADOW)
    assert plan.breakers is not None
    assert (plan.breakers.rolling.status, plan.breakers.hard.status) == (NOT_MEASURED,) * 2
    assert len(buys(plan)) == 3
    assert "rolling-month breaker: n/a" in plan.render()
    assert "hard breaker: n/a" in plan.render()


async def test_a_measured_book_inside_its_limits_buys(rig: Any) -> None:
    await closes(rig, "1000", "1050")
    plan = await make_rebalancer(rig).survey(ExecutionMode.SHADOW)
    assert plan.breakers is not None
    assert (plan.breakers.rolling.status, plan.breakers.hard.status) == (OK, OK)
    assert len(buys(plan)) == 3


async def test_no_buy_while_the_rolling_month_breaker_is_tripped(rig: Any) -> None:
    await closes(rig, "1200", "1200")  # the book is at its cap of 1,000 now: 16.7 % below
    plan = await make_rebalancer(rig).survey(ExecutionMode.SHADOW)
    assert plan.breakers is not None
    assert (plan.breakers.rolling.status, plan.breakers.hard.status) == (TRIPPED_STATE, OK)
    assert plan.blocked is None  # the book is still surveyed: only its buys are refused
    assert not plan.trades
    assert {d.action for d in plan.decisions} == {"skip"}
    assert all(
        d.reason.startswith("buy: the rolling-month breaker is tripped") for d in plan.decisions
    )
    assert "freeze_new_buys_and_audit" in plan.decisions[0].reason
    assert await make_rebalancer(rig).apply(plan) == []


async def test_sales_stay_allowed_while_frozen_and_no_breaker_creates_one(rig: Any) -> None:
    await hold(rig, IBGM, "4", "EUR")  # beyond its band: one is sold back
    await hold(rig, EMIM, "1", "EUR")  # short of its two: one more is bought
    await hold(rig, IWDA, "1", "USD")
    free = await make_rebalancer(rig).survey(ExecutionMode.PAPER)
    assert sells(free) == [(IBGM, Decimal(1))]
    for history in (("1200", "1200"), ("1500", "1500")):  # the freeze, then the hard limit
        await closes(rig, *history, book="broker")
        frozen = await make_rebalancer(rig).survey(ExecutionMode.PAPER)
        assert frozen.breakers is not None
        assert frozen.breakers.buys_refused is not None
        assert sells(frozen) == sells(free)  # the same sale, and no other
        assert buys(frozen) == []
    assert frozen.breakers.hard.status == TRIPPED_STATE
    # A survey only reads: the mode is forced by the cycle's breakers step, never by a plan.
    assert await rig.modes.current() is ExecutionMode.SHADOW
    assert await rig.store(repo.tripped, "broker") is None


async def test_a_stale_close_refuses_new_buys(rig: Any) -> None:
    limit = rig.settings.portfolio_circuit_breaker.max_close_age_days
    await closes(rig, "1000", "1000", last=TODAY - timedelta(days=limit))
    plan = await make_rebalancer(rig).survey(ExecutionMode.SHADOW)
    assert plan.breakers is not None
    assert plan.breakers.rolling.status == UNREADABLE
    assert buys(plan) == []
    assert all("cannot be read" in d.reason and "stale" in d.reason for d in plan.decisions)


async def test_a_missing_nav_makes_the_breakers_unreadable_and_nothing_is_bought(rig: Any) -> None:
    await closes(rig, "1000", "1000")
    rig.broker.nav = None
    plan = await make_rebalancer(rig).survey(ExecutionMode.SHADOW)
    assert plan.breakers is not None
    assert plan.breakers.rolling.status == UNREADABLE
    assert "the managed NAV cannot be read" in str(plan.breakers.buys_refused)
    assert plan.blocked is not None
    assert not plan.trades


async def test_a_store_that_cannot_be_read_refuses_new_buys(
    rig: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(breakers_module, "stored_report", broken)
    plan = await make_rebalancer(rig).survey(ExecutionMode.SHADOW)
    assert plan.breakers is not None
    assert (plan.breakers.rolling.status, plan.breakers.hard.status) == (UNREADABLE,) * 2
    assert "the store cannot be read (OperationalError)" in str(plan.breakers.buys_refused)
    assert buys(plan) == []


async def test_a_latched_book_buys_nothing_in_shadow_either(rig: Any) -> None:
    await closes(rig, "1000", "1000")
    parts = capital_parts(rig)
    report = await parts.breakers.observe(ExecutionMode.SHADOW, close=False)
    assert report.buys_refused is None
    await parts.breakers.latch(report)
    plan = await make_rebalancer(rig).survey(ExecutionMode.SHADOW)
    assert buys(plan) == []
    assert "obliskiq breakers reset" in plan.decisions[0].reason


# ── a cycle's reading ──


def watcher(rig: Any) -> Breakers:
    return capital_parts(rig).breakers


async def test_a_cycles_reading_is_stored_with_the_reference_and_only_the_last_is_a_close(
    rig: Any,
) -> None:
    breakers = watcher(rig)
    assert breakers.closes("us_late")
    assert not breakers.closes("europe")
    await breakers.observe(ExecutionMode.SHADOW, close=False)
    (morning,) = await rig.store(repo.readings, "shadow", as_of=TODAY)
    assert (morning.managed, morning.is_close) == (Decimal(1000), False)
    assert (morning.reference_conid, morning.reference_unit) == (IWDA, IWDA_EUR)
    assert await rig.store(repo.readings, "broker", as_of=TODAY) == []
    rig.clock.now = OPEN + timedelta(hours=10)
    await breakers.observe(ExecutionMode.SHADOW, close=True)
    (close,) = await rig.store(repo.readings, "shadow", as_of=TODAY)
    assert close.is_close
    assert close.recorded_at == OPEN + timedelta(hours=10)


async def test_an_intraday_nav_never_raises_the_peak(rig: Any) -> None:
    await closes(rig, "1000", "1000", last=TODAY)
    breakers = watcher(rig)
    # Inside the day the book reads 1,300 (a deposit the cap follows would do it; here the cap).
    high = rig.settings.model_copy(
        update={"capital": rig.settings.capital.model_copy(update={"cap_base": Decimal(1300)})}
    )
    spike = capital_parts(rig, high).breakers
    await spike.observe(ExecutionMode.SHADOW, close=False)
    (_, _, today) = await rig.store(repo.readings, "shadow", as_of=TODAY)
    assert (today.managed, today.is_close) == (Decimal(1300), False)
    # The next day the book is at 1,000 again: measured against the closes, not against 1,300.
    rig.clock.now = OPEN + timedelta(days=1)
    report = await breakers.observe(ExecutionMode.SHADOW, close=False)
    assert report.rolling.status == OK
    assert report.rolling.worst is not None
    assert report.rolling.worst.fall == 0
    assert report.rolling.closes == 2


async def test_a_dry_run_stores_no_reading_and_a_reference_without_a_quote_is_stored_as_none(
    rig: Any, shipped_settings: ObliskConfig
) -> None:
    await watcher(rig).observe(ExecutionMode.SHADOW, close=True, dry_run=True)
    assert await rig.store(repo.readings, "shadow", as_of=TODAY) == []
    # The shipped reference (XDWD) has no quote at the fake broker.
    unquoted = capital_parts(rig, three_fund_core(shipped_settings)).breakers
    await unquoted.observe(ExecutionMode.SHADOW, close=True)
    (stored,) = await rig.store(repo.readings, "shadow", as_of=TODAY)
    assert (stored.reference_conid, stored.reference_unit) == (None, None)


async def test_an_account_that_cannot_be_read_is_an_unreadable_breaker_and_stores_nothing(
    rig: Any,
) -> None:
    rig.broker.unavailable.add("ledger")
    report = await watcher(rig).observe(ExecutionMode.SHADOW, close=True)
    assert report.rolling.status == UNREADABLE
    assert "the managed NAV cannot be read" in report.rolling.reason
    assert report.buys_refused is not None
    assert await rig.store(repo.readings, "shadow", as_of=TODAY) == []


async def test_a_managed_value_that_is_no_value_is_an_unreadable_breaker(rig: Any) -> None:
    parts = capital_parts(rig)
    cash = await parts.capital.cash()
    view = await parts.capital.read(ExecutionMode.SHADOW, cash=cash)
    assert not isinstance(view, str)
    emptied = dataclasses.replace(view, managed=Decimal(0))
    report = await parts.breakers.read(ExecutionMode.SHADOW, emptied, cash)
    assert report.rolling.status == UNREADABLE
    assert "a managed NAV of 0 is not a book's value" in str(report.buys_refused)
