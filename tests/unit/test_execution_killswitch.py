"""The kill switch: two flags that fail closed, and the sweep that cancels what adds risk (I4).

Adapted from the legacy monitoring suite (legacy/old/ObliskIQ/tests/test_monitoring_suite.py,
``test_should_halt_reflexivity_*``, and tests/test_kill_switch_wiring.py): the legacy check
answered "allowed" when its status could not be read; here a switch that cannot be read is ON.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from order_rig import terms

from obliskiq.domain.orders import IntentRole, OrderSide, RiskEffect
from obliskiq.errors import ExecutionError, StoreError
from obliskiq.execution import killswitch as module
from obliskiq.execution.killswitch import (
    KILL_SWITCH_FILE,
    file_flag_on,
    kill_switch_path,
    set_file_flag,
)
from obliskiq.execution.pretrade import PretradeGate, PretradeRefusal
from obliskiq.ibkr.errors import IbkrUpstreamError


async def _place(rig: Any, order_terms: Any, *, role: IntentRole, reason: str, effect: Any) -> Any:
    stored = await rig.intent(order_terms, role=role, reason=reason)
    result = await rig.boundary.place(
        stored.intent.intent_id, await rig.clear(stored.intent, effect=effect)
    )
    return stored, result.order_id


# ── the flags ──


async def test_the_switch_is_off_until_set(order_rig: Any) -> None:
    assert await order_rig.kill.is_on() is False


async def test_either_flag_alone_turns_it_on(order_rig: Any) -> None:
    set_file_flag(order_rig.data_dir, actor="owner", reason="file only", at=order_rig.clock())
    assert await order_rig.kill.is_on() is True
    kill_switch_path(order_rig.data_dir).unlink()
    assert await order_rig.kill.is_on() is False
    problems = await order_rig.kill.turn_on(actor="owner", reason="both")
    assert problems == ()
    kill_switch_path(order_rig.data_dir).unlink()
    assert await order_rig.kill.is_on() is True  # the database flag alone


async def test_the_file_flag_records_who_and_why(order_rig: Any) -> None:
    await order_rig.kill.turn_on(actor="owner", reason="drill")
    content = json.loads(kill_switch_path(order_rig.data_dir).read_text(encoding="utf-8"))
    assert content["actor"] == "owner"
    assert content["reason"] == "drill"
    assert kill_switch_path(order_rig.data_dir).name == KILL_SWITCH_FILE


async def test_a_database_that_cannot_be_read_counts_as_on(order_rig: Any) -> None:
    async def broken(*args: Any, **kwargs: Any) -> Any:
        raise StoreError("database is locked")

    order_rig.kill._worker = type("Broken", (), {"run": staticmethod(broken)})()
    assert await order_rig.kill.is_on() is True


async def test_a_database_read_that_hangs_counts_as_on(order_rig: Any) -> None:
    async def hang(*args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(3600)

    order_rig.kill._worker = type("Hung", (), {"run": staticmethod(hang)})()
    order_rig.kill._timeout = 0.01
    assert await order_rig.kill.is_on() is True


def test_a_file_flag_that_cannot_be_checked_counts_as_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A symlink loop: ``Path.exists`` would read it as absent, ``stat`` raises ELOOP.
    kill_switch_path(tmp_path).symlink_to(KILL_SWITCH_FILE)
    assert file_flag_on(tmp_path) is True
    other = tmp_path / "other"
    other.mkdir()

    def refuse(self: Path, *args: Any, **kwargs: Any) -> Any:
        raise PermissionError("no access")

    monkeypatch.setattr(Path, "stat", refuse)
    assert file_flag_on(other) is True


def test_a_missing_file_flag_is_off(tmp_path: Path) -> None:
    assert file_flag_on(tmp_path) is False
    assert file_flag_on(tmp_path / "no-such-directory") is False


async def test_one_flag_that_cannot_be_written_still_turns_it_on(order_rig: Any) -> None:
    def refuse(*args: Any, **kwargs: Any) -> None:
        raise OSError("read-only file system")

    original = module.set_file_flag
    module.set_file_flag = refuse
    try:
        problems = await order_rig.kill.turn_on(actor="owner", reason="drill")
    finally:
        module.set_file_flag = original
    assert len(problems) == 1
    assert "file flag" in problems[0]
    assert await order_rig.kill.is_on() is True


async def test_no_flag_written_is_an_error(order_rig: Any) -> None:
    def refuse(*args: Any, **kwargs: Any) -> None:
        raise OSError("read-only file system")

    async def broken(*args: Any, **kwargs: Any) -> Any:
        raise StoreError("database is locked")

    original = module.set_file_flag
    module.set_file_flag = refuse
    order_rig.kill._worker = type("Broken", (), {"run": staticmethod(broken)})()
    try:
        with pytest.raises(ExecutionError):
            await order_rig.kill.turn_on(actor="owner", reason="drill")
    finally:
        module.set_file_flag = original


async def test_turning_off_clears_both_flags(order_rig: Any) -> None:
    await order_rig.kill.turn_on(actor="owner", reason="drill")
    await order_rig.kill.turn_off(actor="owner", reason="drill over")
    assert await order_rig.kill.is_on() is False
    assert not kill_switch_path(order_rig.data_dir).exists()


async def test_turning_off_fails_loudly_when_a_flag_stays(order_rig: Any) -> None:
    await order_rig.kill.turn_on(actor="owner", reason="drill")
    original = module.clear_file_flag
    module.clear_file_flag = lambda _data_dir: None  # the file cannot be removed
    try:
        with pytest.raises(ExecutionError):
            await order_rig.kill.turn_off(actor="owner", reason="drill over")
    finally:
        module.clear_file_flag = original
    assert await order_rig.kill.is_on() is True


# ── the sweep (I4) ──


async def test_on_cancels_what_adds_risk_and_keeps_the_sells_that_reduce_it(
    order_rig: Any,
) -> None:
    _, buy_id = await _place(
        order_rig, terms(), role=IntentRole.PROTECTIVE, reason="stop", effect=RiskEffect.ADDS
    )
    _, conversion_id = await _place(
        order_rig,
        terms(cash="5000", conversion=True, conid=12087792),
        role=IntentRole.CONVERSION,
        reason="fund a trade",
        effect=RiskEffect.ADDS,
    )
    _, exit_id = await _place(
        order_rig,
        terms(side=OrderSide.SELL, quantity="4"),
        role=IntentRole.EXIT,
        reason="target",
        effect=RiskEffect.REDUCES,
    )
    _, stop_id = await _place(
        order_rig,
        terms(side=OrderSide.SELL, quantity="6", stop="95", limit="94"),
        role=IntentRole.PROTECTIVE,
        reason="stop",
        effect=RiskEffect.REDUCES,
    )
    await order_rig.kill.turn_on(actor="owner", reason="drill")
    report = await order_rig.kill.sweep(order_rig.client)
    assert set(report.cancelled) == {buy_id, conversion_id}
    assert set(report.kept) == {exit_id, stop_id}
    assert order_rig.broker.orders[exit_id].status == "Submitted"
    assert order_rig.broker.orders[stop_id].status == "Submitted"
    assert report.failed == ()


async def test_a_sell_the_gate_cleared_as_adding_risk_is_cancelled(order_rig: Any) -> None:
    _, sell_id = await _place(
        order_rig,
        terms(side=OrderSide.SELL),
        role=IntentRole.EXIT,
        reason="target",
        effect=RiskEffect.ADDS,
    )
    await order_rig.kill.turn_on(actor="owner", reason="drill")
    report = await order_rig.kill.sweep(order_rig.client)
    assert report.cancelled == (sell_id,)


async def test_orders_the_store_does_not_know_are_classed_from_the_broker(order_rig: Any) -> None:
    manual_buy = order_rig.broker.add_order(side="BUY")
    manual_sell = order_rig.broker.add_order(side="SELL")
    unknown_coid_sell = order_rig.broker.add_order(side="SELL", coid="not-ours")
    fx = order_rig.broker.add_order(side="SELL", sec_type="CASH")
    no_side = order_rig.broker.add_order(side="?")
    filled = order_rig.broker.add_order(side="BUY", status="Filled")
    report = await order_rig.kill.sweep(order_rig.client)
    assert set(report.cancelled) == {manual_buy.order_id, fx.order_id, no_side.order_id}
    assert set(report.kept) == {manual_sell.order_id, unknown_coid_sell.order_id}
    assert filled.order_id not in report.cancelled + report.kept
    assert set(report.classed_by_broker) == {
        manual_buy.order_id,
        manual_sell.order_id,
        unknown_coid_sell.order_id,
        fx.order_id,
        no_side.order_id,
    }


async def test_with_the_store_unavailable_orders_are_classed_from_the_broker(
    order_rig: Any,
) -> None:
    _, buy_id = await _place(
        order_rig, terms(), role=IntentRole.PROTECTIVE, reason="stop", effect=RiskEffect.ADDS
    )
    _, sell_id = await _place(
        order_rig,
        terms(side=OrderSide.SELL),
        role=IntentRole.EXIT,
        reason="target",
        effect=RiskEffect.ADDS,
    )

    async def broken(*args: Any, **kwargs: Any) -> Any:
        raise StoreError("the store is unavailable")

    order_rig.kill._worker = type("Broken", (), {"run": staticmethod(broken)})()
    report = await order_rig.kill.sweep(order_rig.client)
    assert report.cancelled == (buy_id,)
    assert report.kept == (sell_id,)  # the store would have cancelled it; the broker keeps a sell
    assert set(report.classed_by_broker) == {buy_id, sell_id}


async def test_the_sweep_works_while_the_gate_refuses_everything(order_rig: Any) -> None:
    stored, buy_id = await _place(
        order_rig, terms(), role=IntentRole.ENTRY, reason="entry", effect=RiskEffect.ADDS
    )
    assert isinstance(await PretradeGate().clear(stored.intent), PretradeRefusal)
    await order_rig.kill.turn_on(actor="owner", reason="drill")
    assert (await order_rig.kill.sweep(order_rig.client)).cancelled == (buy_id,)


async def test_an_order_placed_just_before_on_and_listed_late_is_caught(order_rig: Any) -> None:
    order_rig.broker.listing_delay = 1
    _, buy_id = await _place(
        order_rig, terms(), role=IntentRole.ENTRY, reason="entry", effect=RiskEffect.ADDS
    )
    await order_rig.kill.turn_on(actor="owner", reason="drill")
    first = await order_rig.kill.sweep(order_rig.client)
    assert first.cancelled == ()
    second = await order_rig.kill.sweep(order_rig.client)
    assert second.cancelled == (buy_id,)


async def test_the_sweep_repeats_while_on_and_stops_when_asked(order_rig: Any) -> None:
    order_rig.broker.listing_delay = 1
    _, buy_id = await _place(
        order_rig, terms(), role=IntentRole.ENTRY, reason="entry", effect=RiskEffect.ADDS
    )
    await order_rig.kill.turn_on(actor="owner", reason="drill")
    order_rig.kill._interval = 0.001
    reports: list[Any] = []
    stop = asyncio.Event()

    def collect(report: Any) -> None:
        reports.append(report)
        if report.cancelled:
            stop.set()

    await asyncio.wait_for(order_rig.kill.run(order_rig.client, stop, on_report=collect), 5)
    assert reports[0].cancelled == ()
    assert reports[-1].cancelled == (buy_id,)


async def test_the_loop_does_not_sweep_while_off(order_rig: Any) -> None:
    order_rig.broker.add_order(side="BUY")
    order_rig.kill._interval = 0.001
    stop = asyncio.Event()
    calls: list[Any] = []

    async def stop_soon() -> None:
        await asyncio.sleep(0.02)
        stop.set()

    await asyncio.gather(
        order_rig.kill.run(order_rig.client, stop, on_report=calls.append), stop_soon()
    )
    assert calls == []
    assert order_rig.broker.cancels == []


async def test_a_sweep_that_cannot_read_the_orders_reports_it(order_rig: Any) -> None:
    async def fail() -> Any:
        raise IbkrUpstreamError("down")

    order_rig.client.live_order_rows = fail
    report = await order_rig.kill.sweep(order_rig.client)
    assert report.error is not None
    assert report.cancelled == ()


async def test_a_cancel_that_fails_is_reported_and_retried_next_sweep(order_rig: Any) -> None:
    refused = order_rig.broker.add_order(side="BUY")
    broken = order_rig.broker.add_order(side="BUY")
    original = order_rig.client.cancel

    async def cancel(order_id: str) -> Any:
        if order_id == broken.order_id:
            raise IbkrUpstreamError("down")
        answer = await original(order_id)
        return type(answer)(order_id=order_id, message=None, error="refused")

    order_rig.client.cancel = cancel
    report = await order_rig.kill.sweep(order_rig.client)
    assert len(report.failed) == 2
    assert report.cancelled == ()
    assert refused.order_id in report.failed[0] + report.failed[1]


async def test_the_store_overrules_a_broker_sell_it_knows_as_a_buy(order_rig: Any) -> None:
    buy = await order_rig.intent()  # the store holds a buy under this cOID
    odd = order_rig.broker.add_order(side="SELL", coid=buy.coid)
    sell = await order_rig.intent(terms(side=OrderSide.SELL), role=IntentRole.EXIT, reason="target")
    uncleared = order_rig.broker.add_order(side="SELL", coid=sell.coid)  # no clearance spent
    report = await order_rig.kill.sweep(order_rig.client)
    assert report.cancelled == (odd.order_id,)
    assert report.kept == (uncleared.order_id,)
    assert report.classed_by_broker == (uncleared.order_id,)


async def test_the_loop_runs_without_a_report_hook(order_rig: Any) -> None:
    order = order_rig.broker.add_order(side="BUY")
    await order_rig.kill.turn_on(actor="owner", reason="drill")
    order_rig.kill._interval = 0.001
    stop = asyncio.Event()

    async def stop_when_cancelled() -> None:
        while order.order_id not in order_rig.broker.cancels:
            await asyncio.sleep(0.001)
        stop.set()

    await asyncio.wait_for(
        asyncio.gather(order_rig.kill.run(order_rig.client, stop), stop_when_cancelled()), 5
    )


async def test_one_unreadable_row_does_not_stop_the_sweep(order_rig: Any) -> None:
    buy = order_rig.broker.add_order(side="BUY")
    odd = order_rig.broker.add_order(side="SELL")

    def corrupt(rows: list[Any]) -> list[Any]:
        for row in rows:
            if row["orderId"] == int(odd.order_id):
                row["conid"] = 0  # fails validation
        return [*rows, {"status": "Submitted", "side": "BUY"}, "not an object"]

    order_rig.broker.rewrite_live_rows = corrupt
    report = await order_rig.kill.sweep(order_rig.client)
    # The unreadable sell is cancelled too: nothing proves it reduces risk.
    assert set(report.cancelled) == {buy.order_id, odd.order_id}
    assert len(report.failed) == 2
    assert odd.order_id in report.classed_by_broker


async def test_a_cancel_that_raises_anything_is_a_failed_cancel(order_rig: Any) -> None:
    order = order_rig.broker.add_order(side="BUY")

    async def refuse(order_id: str) -> Any:
        raise RuntimeError("the paper guard or anything else")

    order_rig.client.cancel = refuse
    report = await order_rig.kill.sweep(order_rig.client)
    assert report.failed == (f"{order.order_id} (RuntimeError)",)


async def test_the_loop_survives_a_failing_hook_and_a_failing_pass(order_rig: Any) -> None:
    order_rig.broker.add_order(side="BUY")
    await order_rig.kill.turn_on(actor="owner", reason="drill")
    order_rig.kill._interval = 0.001
    stop = asyncio.Event()
    passes = 0
    original = order_rig.kill.sweep

    async def flaky(client: Any) -> Any:
        nonlocal passes
        passes += 1
        if passes == 2:
            raise RuntimeError("an unexpected failure inside a pass")
        if passes >= 4:
            stop.set()
        return await original(client)

    def broken_hook(report: Any) -> None:
        raise RuntimeError("telegram is down")

    order_rig.kill.sweep = flaky
    await asyncio.wait_for(order_rig.kill.run(order_rig.client, stop, on_report=broken_hook), 5)
    assert passes >= 4
