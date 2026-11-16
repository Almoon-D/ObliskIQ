"""The reconciliation freeze (docs/AUDIT.md, 1; DECISIONS D179): new risk needs a green
reconciliation that exists and is recent. Before the first run, and once the last green run has
aged, the gate refuses orders that add risk, the executor stages none and the rebalancers plan
none; sells that reduce risk go on, and a dry run still shows its plan.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import respx
from fake_broker import FakeBroker
from order_rig import STOCK, build_rig, terms
from pydantic import ValidationError

from obliskiq.calendars.service import Calendars
from obliskiq.config import ObliskConfig
from obliskiq.domain.orders import (
    IntentRole,
    IntentState,
    OrderSide,
    PretradeClearance,
    ReconcileFinding,
    ReconcileRun,
    ReconcileStatus,
)
from obliskiq.execution.freeze import new_risk_frozen
from obliskiq.execution.pretrade import PretradeRefusal, Reason

AT = datetime(2026, 10, 13, 14, 30, tzinfo=UTC)
MAX_AGE = timedelta(seconds=900)


def run(status: ReconcileStatus, at: datetime = AT) -> ReconcileRun:
    findings = (
        ()
        if status is ReconcileStatus.GREEN
        else (ReconcileFinding(kind="test", detail="a test finding", freezes=True),)
    )
    return ReconcileRun(at=at, status=status, findings=findings)


def test_only_a_recent_green_run_lets_new_risk_through() -> None:
    green = run(ReconcileStatus.GREEN)
    assert new_risk_frozen(green, now=AT, max_age=MAX_AGE) is None
    assert new_risk_frozen(green, now=AT + MAX_AGE, max_age=MAX_AGE) is None  # at the limit
    aged = new_risk_frozen(green, now=AT + MAX_AGE + timedelta(seconds=1), max_age=MAX_AGE)
    assert aged is not None
    assert "is 901 s old, beyond 900 s" in aged
    never = new_risk_frozen(None, now=AT, max_age=MAX_AGE)
    assert never == "no reconciliation has run yet: new risk waits for a green one"
    for status in (ReconcileStatus.DRIFT, ReconcileStatus.INCOMPLETE):
        frozen = new_risk_frozen(run(status), now=AT, max_age=MAX_AGE)
        assert frozen is not None
        assert f"is {status.value}" in frozen


def test_a_rehearsal_is_blocked_by_drift_alone() -> None:
    late = AT + timedelta(days=3)
    green = run(ReconcileStatus.GREEN)
    assert new_risk_frozen(None, now=AT, max_age=MAX_AGE, rehearsal=True) is None
    assert new_risk_frozen(green, now=late, max_age=MAX_AGE, rehearsal=True) is None
    for status in (ReconcileStatus.DRIFT, ReconcileStatus.INCOMPLETE):
        assert new_risk_frozen(run(status), now=AT, max_age=MAX_AGE, rehearsal=True) is not None


def test_a_green_run_must_outlive_a_heartbeat(shipped_settings: ObliskConfig) -> None:
    """Reconciliation runs once a heartbeat: a shorter life would freeze new risk between them."""
    reconcile = shipped_settings.execution.reconcile
    assert reconcile.max_age_seconds > shipped_settings.runtime.heartbeat_seconds
    raw = shipped_settings.model_dump()
    raw["execution"]["reconcile"]["max_age_seconds"] = shipped_settings.runtime.heartbeat_seconds
    with pytest.raises(ValidationError, match=r"max_age_seconds .* must exceed"):
        ObliskConfig.model_validate(raw)


@pytest.fixture
async def rig(
    make_session: Callable[..., Any],
    shipped_settings: ObliskConfig,
    rig_calendars: Calendars,
    tmp_path: Path,
) -> AsyncIterator[Any]:
    """The order path of a machine that has never reconciled."""
    with respx.mock(assert_all_called=False) as router:
        built = await build_rig(
            settings=shipped_settings,
            session=make_session(),
            broker=FakeBroker(router),
            tmp_path=tmp_path,
            calendars=rig_calendars,
            reconciled=False,
        )
        await built.to_paper()
        built.ready()
        built.broker.settle_cash = True
        built.make_execution()
        yield built
        await built.aclose()


async def test_the_gate_refuses_new_risk_before_the_first_reconciliation(rig: Any) -> None:
    await rig.hold(STOCK, "10")
    stop = await rig.staged(
        side=OrderSide.SELL, stop="95", limit="94.5", role=IntentRole.PROTECTIVE
    )
    buy = await rig.staged()
    answer = await rig.gate.clear(buy.intent)
    assert isinstance(answer, PretradeRefusal)
    assert answer.codes == (Reason.RECONCILIATION_FROZEN.value,)
    assert "no reconciliation has run yet" in answer.reason
    # An order that reduces risk is never frozen: the position keeps its stop.
    assert isinstance(await rig.gate.clear(stop.intent), PretradeClearance)
    assert (await rig.reconciler.run()).status is ReconcileStatus.GREEN
    assert isinstance(await rig.gate.clear(buy.intent), PretradeClearance)


async def test_the_gate_refuses_new_risk_once_the_green_run_has_aged(rig: Any) -> None:
    await rig.reconciled()
    max_age = rig.settings.execution.reconcile.max_age_seconds
    rig.clock.advance(max_age)
    assert isinstance(await rig.gate.clear((await rig.staged()).intent), PretradeClearance)
    rig.clock.advance(1)
    answer = await rig.gate.clear((await rig.staged()).intent)
    assert isinstance(answer, PretradeRefusal)
    assert answer.codes == (Reason.RECONCILIATION_FROZEN.value,)
    assert f"beyond {max_age} s" in answer.reason
    await rig.reconciled()
    assert isinstance(await rig.gate.clear((await rig.staged()).intent), PretradeClearance)


async def test_the_executor_places_no_buy_until_it_has_reconciled(rig: Any) -> None:
    """The first cycle of a new machine, or the first after an outage: the buy waits for the
    cycle's own reconciliation and is placed by the next."""
    buy = await rig.intent(
        terms(quantity="10", limit="99.95", conid=STOCK),
        role=IntentRole.ENTRY,
        reason="entry",
        state=IntentState.PROPOSED,
    )

    async def state() -> IntentState:
        return (await rig.book.get(buy.intent.intent_id)).intent.state  # type: ignore[no-any-return]

    first = await rig.executor.cycle()
    assert await state() is IntentState.APPROVED
    assert rig.broker.placements == []
    assert first.reconcile is not None
    assert first.reconcile.status is ReconcileStatus.GREEN
    await rig.executor.cycle()
    assert await state() is IntentState.WORKING
    assert len(rig.broker.placements) == 1


async def test_a_buy_staged_before_the_freeze_is_neither_funded_nor_placed_while_it_lasts(
    rig: Any,
) -> None:
    """A dollar buy staged before an outage: on the first cycle after it no conversion is sent
    for it (the gate would refuse it, with an alert); the cycle reconciles, the next one funds
    and places."""
    await rig.reconciled()
    rig.broker.cash["USD"] = (rig.broker.cash["USD"][0] * 0, rig.broker.cash["USD"][1])
    buy = await rig.staged()
    rig.clock.advance(rig.settings.execution.reconcile.max_age_seconds + 1)
    report = await rig.executor.cycle()
    (step,) = [s for s in report.steps if s.intent_id == buy.intent.intent_id]
    assert (step.action, step.detail) == ("frozen", "reconciliation freezes new risk")
    assert rig.broker.placements == []
    assert report.conversions == []
    assert rig.alerts == []
    assert (await rig.book.get(buy.intent.intent_id)).intent.state is IntentState.STAGED
    after = await rig.executor.cycle()
    actions = [s.action for s in after.steps if s.intent_id == buy.intent.intent_id]
    assert actions
    assert "frozen" not in actions  # the cycle's own reconciliation lifted the freeze
