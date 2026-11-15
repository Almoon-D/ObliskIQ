"""Session 25's acceptance: a falling managed NAV driven through the core's cycle freezes the
book's new buys, then forces the mode to shadow, and the run log shows both; the trip survives a
restart, nothing is ever sold, and a dry run changes nothing.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import sqlite3
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from order_rig import whole_account
from service_rig import IWDA, ServiceRig

from obliskiq.config import ObliskConfig
from obliskiq.decision.breakers import NOT_MEASURED, OK, TRIPPED_STATE, release
from obliskiq.domain.orders import ExecutionMode, IntentState, OrderSide
from obliskiq.errors import ModeChangeRefusedError
from obliskiq.execution.modes import forced_shadow_on
from obliskiq.reporting import digest as digest_module
from obliskiq.store import breakers as repo
from obliskiq.store import orders as orders_repo
from obliskiq.store import runs as run_repo

MONDAY, TUESDAY, WEDNESDAY, THURSDAY = (date(2026, 10, day) for day in (26, 27, 28, 29))
#: An hour after Xetra's open and an hour before New York's close, in UTC, that week.
EUROPE_HOUR, US_LATE_HOUR = 9, 19


def falling(settings: ObliskConfig) -> ObliskConfig:
    """The whole account is the managed book (its NAV is the fake broker's) and the equity
    reference is IWDA, which the fake broker quotes and holds flat."""
    breaker = settings.portfolio_circuit_breaker.model_copy(update={"equity_reference_conid": IWDA})
    return whole_account(settings).model_copy(update={"portfolio_circuit_breaker": breaker})


async def cycle(s: ServiceRig, name: str, day: date, nav: int, **kw: Any) -> Any:
    hour = US_LATE_HOUR if name == "us_late" else EUROPE_HOUR
    s.clock.wall.now = datetime(day.year, day.month, day.day, hour, 0, tzinfo=UTC)
    s.broker.nav = Decimal(nav)
    return await s.service.jobs.cycle(name, day, **kw)


async def breakers_step(s: ServiceRig, name: str, day: date) -> dict[str, Any]:
    (run,) = await s.rig.store(run_repo.runs_for, command=f"cycle:{name}", session_date=day)
    return next(step for step in run.detail["steps"] if step["step"] == "breakers")


async def sells(s: ServiceRig) -> list[Any]:
    every = await s.rig.store(orders_repo.intents_in_states, list(IntentState))
    return [i for i in every if i.intent.side is OrderSide.SELL]


async def test_a_falling_nav_freezes_new_buys_then_forces_shadow_and_the_run_log_shows_both(
    make: Any, shipped_settings: ObliskConfig
) -> None:
    settings = falling(shipped_settings)
    s: ServiceRig = await make(paper=True, settings=settings)
    s.clock.wall.now = datetime(2026, 10, 26, US_LATE_HOUR, 0, tzinfo=UTC)
    await s.service.prepare()

    # Two daily closes at 100,000: the first is too few to measure, and says so.
    first = await cycle(s, "us_late", MONDAY, 100_000)
    assert first.breakers.rolling.status == NOT_MEASURED
    assert (await breakers_step(s, "us_late", MONDAY))["status"] == "ok"
    await cycle(s, "us_late", TUESDAY, 100_000)

    # Wednesday morning the book is 16 % down and the equity reference flat: the freeze.
    frozen = await cycle(s, "europe", WEDNESDAY, 84_000)
    assert (frozen.breakers.rolling.status, frozen.breakers.hard.status) == (TRIPPED_STATE, OK)
    step = await breakers_step(s, "europe", WEDNESDAY)
    assert step["status"] == "degraded"
    assert step["reason"].startswith("new buys frozen: the rolling-month breaker is tripped")
    assert (step["rolling"], step["hard"], step["mode"]) == ("tripped", "ok", "paper")
    assert frozen.plan.blocked is None
    assert not frozen.plan.trades  # an empty book that would buy every role buys none
    assert all(d.reason.startswith("buy: the rolling-month") for d in frozen.plan.decisions)
    assert frozen.made == []
    assert await s.path.modes.current() is ExecutionMode.PAPER  # a freeze changes no mode
    (alert,) = s.notifier.alerts("breaker.frozen")
    assert "Sales stay allowed and nothing is sold" in alert

    # That evening it is 30 % down: the hard breaker forces shadow, latches and alerts.
    forced = await cycle(s, "us_late", WEDNESDAY, 70_000)
    assert forced.breakers.newly_tripped
    step = await breakers_step(s, "us_late", WEDNESDAY)
    assert step["status"] == "degraded"
    assert step["reason"].startswith("forced shadow: the hard breaker tripped")
    assert (step["hard"], step["mode"]) == ("tripped", "shadow")
    assert await s.path.modes.current() is ExecutionMode.SHADOW
    change = await s.rig.store(orders_repo.latest_mode_change)
    assert (change.actor, change.direction) == ("breaker", "downgrade")
    assert not forced_shadow_on(s.rig.data_dir)  # the store took the audit row
    assert forced.plan.mode is ExecutionMode.SHADOW  # the rest of the cycle ran in shadow
    (alert,) = s.notifier.alerts("breaker.hard_tripped")
    assert "mode paper -> shadow" in alert
    assert "Nothing is sold" in alert
    assert "obliskiq breakers reset" in alert
    latch = await s.rig.store(repo.tripped, "broker")
    assert latch is not None
    assert "30.0% beyond it, limit 25.0%" in latch.detail

    # No breaker made a sale, and no order for a security reached the broker (the cycles'
    # only placements are the currency manager's conversions of idle dollars).
    assert await sells(s) == []
    assert s.placed() == []

    # The trip survives a restart: a new service over the same store is still stood down.
    restarted: ServiceRig = await make(settings=settings)
    assert await restarted.path.modes.current() is ExecutionMode.SHADOW
    assert await restarted.rig.store(repo.tripped, "broker") == latch
    with pytest.raises(ModeChangeRefusedError, match="obliskiq breakers reset"):
        await restarted.path.modes.upgrade(ExecutionMode.PAPER, actor="owner", reason="back")
    await cycle(restarted, "europe", THURSDAY, 70_000)
    assert (await breakers_step(restarted, "europe", THURSDAY))["mode"] == "shadow"
    assert restarted.notifier.alerts("breaker.hard_tripped") == []  # said once, not again

    # The digest shows each breaker's state, and the trip that holds on the broker's book.
    await restarted.service.jobs.digest(THURSDAY)
    (digest,) = [body for subject, body in restarted.notifier.sent if "daily digest" in subject]
    assert "Breakers (shadow book):" in digest
    assert "rolling-month breaker: n/a" in digest
    assert "hard breaker of the broker book: TRIPPED" in digest

    # The owner's reset releases the latch; the upgrade is then theirs to make.
    released = await restarted.rig.store(
        lambda store: release(store, at=restarted.clock.now(), actor="owner", reason="looked")
    )
    assert released == ["broker"]
    await restarted.path.modes.upgrade(ExecutionMode.PAPER, actor="owner", reason="back")
    back = await cycle(restarted, "us_late", THURSDAY, 70_000)
    assert back.breakers.hard.status == NOT_MEASURED  # it measures from the closes that follow
    assert back.breakers.rolling.status == TRIPPED_STATE  # the month still holds 100,000
    assert await restarted.path.modes.current() is ExecutionMode.PAPER


async def test_a_dry_run_reads_and_judges_and_changes_nothing(
    make: Any, shipped_settings: ObliskConfig
) -> None:
    s: ServiceRig = await make(paper=True, settings=falling(shipped_settings))
    s.clock.wall.now = datetime(2026, 10, 26, US_LATE_HOUR, 0, tzinfo=UTC)
    await s.service.prepare()
    await cycle(s, "us_late", MONDAY, 100_000)
    await cycle(s, "us_late", TUESDAY, 100_000)
    dry = await cycle(s, "us_late", WEDNESDAY, 70_000, dry_run=True)
    assert dry.breakers.newly_tripped  # the plan says what a real cycle would find
    assert await s.path.modes.current() is ExecutionMode.PAPER
    assert await s.rig.store(repo.tripped, "broker") is None
    assert [r.as_of for r in await s.rig.store(repo.readings, "broker", as_of=WEDNESDAY)] == [
        MONDAY,
        TUESDAY,
    ]
    assert s.notifier.alerts("breaker.hard_tripped") == []


async def test_a_stand_down_the_store_cannot_record_is_held_by_the_file_and_said_so(
    make: Any, shipped_settings: ObliskConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    s: ServiceRig = await make(paper=True, settings=falling(shipped_settings))
    s.clock.wall.now = datetime(2026, 10, 26, US_LATE_HOUR, 0, tzinfo=UTC)
    await s.service.prepare()
    await cycle(s, "us_late", MONDAY, 100_000)
    await cycle(s, "us_late", TUESDAY, 100_000)

    async def refused(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("the audit row cannot be written")

    monkeypatch.setattr(s.path.modes, "downgrade", refused)
    await cycle(s, "us_late", WEDNESDAY, 70_000)
    assert forced_shadow_on(s.rig.data_dir)
    assert await s.path.modes.current() is ExecutionMode.SHADOW
    (alert,) = s.notifier.alerts("breaker.hard_tripped")
    assert "held by the FORCED_SHADOW file" in alert
    assert await s.rig.store(repo.tripped, "broker") is not None


async def test_an_unreadable_breaker_alerts_and_the_cycle_is_not_a_failure(
    make: Any, shipped_settings: ObliskConfig
) -> None:
    s: ServiceRig = await make(settings=falling(shipped_settings))
    s.clock.wall.now = datetime(2026, 10, 26, US_LATE_HOUR, 0, tzinfo=UTC)
    await s.service.prepare()
    await cycle(s, "us_late", MONDAY, 100_000)
    await cycle(s, "us_late", TUESDAY, 100_000)
    limit = shipped_settings.portfolio_circuit_breaker.max_close_age_days
    late = TUESDAY + timedelta(days=limit + 1)  # no close for longer than the limit
    stale = await cycle(s, "europe", late, 100_000)
    assert stale.breakers.buys_refused is not None
    assert "stale" in stale.breakers.buys_refused
    assert stale.status in ("ok", "degraded")
    (alert,) = s.notifier.alerts("breaker.unreadable")
    assert "new buys are refused" in alert


async def test_a_shadow_book_that_trips_is_stood_down_where_it_is(
    make: Any, shipped_settings: ObliskConfig
) -> None:
    s: ServiceRig = await make(settings=falling(shipped_settings))
    s.clock.wall.now = datetime(2026, 10, 26, US_LATE_HOUR, 0, tzinfo=UTC)
    await s.service.prepare()
    await cycle(s, "us_late", MONDAY, 100_000)
    await cycle(s, "us_late", TUESDAY, 100_000)
    await cycle(s, "europe", WEDNESDAY, 70_000)
    (alert,) = s.notifier.alerts("breaker.hard_tripped")
    assert "the shadow book is stood down" in alert
    assert await s.rig.store(repo.tripped, "shadow") is not None
    assert await s.rig.store(orders_repo.latest_mode_change) is None  # shadow already
    # The latched book buys nothing afterwards, in shadow either, and says why while it holds.
    after = await cycle(s, "us_late", WEDNESDAY, 100_000)
    assert not after.breakers.newly_tripped
    assert "obliskiq breakers reset" in after.breakers.buys_refused
    assert not after.plan.trades
    assert len(s.notifier.alerts("breaker.frozen")) == 1
    assert len(s.notifier.alerts("breaker.hard_tripped")) == 1


async def test_failed_cycles_stand_the_mode_down_through_the_file_when_the_store_cannot(
    make: Any, shipped_settings: ObliskConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    s: ServiceRig = await make(paper=True, settings=falling(shipped_settings))
    limit = shipped_settings.runtime.downgrade_after_failed_cycles

    async def refused(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("the audit row cannot be written")

    monkeypatch.setattr(s.path.modes, "downgrade", refused)
    for _ in range(limit):
        await s.service._after_cycle("error")
    assert forced_shadow_on(s.rig.data_dir)
    assert await s.path.modes.current() is ExecutionMode.SHADOW
    (alert,) = s.notifier.alerts("service.downgraded")
    assert "held by the FORCED_SHADOW file" in alert
    monkeypatch.undo()
    for _ in range(limit):  # already shadow: nothing changes and nothing more is said
        await s.service._after_cycle("error")
    assert len(s.notifier.alerts("service.downgraded")) == 1


async def test_the_digest_says_when_the_breakers_cannot_be_read(
    make: Any, shipped_settings: ObliskConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    s: ServiceRig = await make(settings=falling(shipped_settings))
    s.clock.wall.now = datetime(2026, 10, 26, US_LATE_HOUR, 0, tzinfo=UTC)

    def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(digest_module, "stored_report", broken)
    await s.service.jobs.digest(MONDAY)
    body = s.notifier.digest()
    assert "Breakers: not read" in body
    assert "cannot read: the breakers cannot be read: OperationalError" in body
