"""Attacks on the kill switch (I4): it must work when everything else fails.

The rule (SESSION_PROMPTS Session 4 item 4, DECISIONS D48): either flag turns it on and a flag
that cannot be read counts as ON; cancel needs no clearance and works in every mode; ON blocks
what adds risk immediately before the send; the sweep cancels every working order that adds risk,
keeps the sells that reduce it, classes from the broker what the store cannot, and repeats while
ON. These tests break the flag check at the operating-system level, feed the sweep a list it can
only partly read, make its report hook fail, run a cancel with the store and the mode gone, and
give the store a stale idea of what a working sell is.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import asyncio
import contextlib
import errno
import os
from collections import deque
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fake_broker import FakeOrder
from order_rig import START, terms

from obliskiq.domain.orders import ClearanceAction, IntentRole, OrderSide, RiskEffect
from obliskiq.errors import StoreError, WriteRefusedError
from obliskiq.execution.killswitch import file_flag_on, kill_switch_path, set_file_flag
from obliskiq.ibkr.orders import PlacementStatus

_UNCHECKABLE = [
    pytest.param(PermissionError(errno.EACCES, "Permission denied"), id="EACCES"),
    pytest.param(OSError(errno.EIO, "Input/output error"), id="EIO"),
]


def _stat_fails_for(monkeypatch: pytest.MonkeyPatch, target: Path, error: OSError) -> None:
    """``stat`` of ``target`` fails as the operating system would; every other path is real."""
    real_stat = os.stat

    def stat(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        if os.fspath(path) == os.fspath(target):
            raise error
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", stat)


async def _place(
    rig: Any,
    order_terms: Any,
    *,
    role: IntentRole = IntentRole.ENTRY,
    reason: str = "entry",
    effect: RiskEffect = RiskEffect.ADDS,
) -> tuple[Any, str]:
    stored = await rig.intent(order_terms, role=role, reason=reason)
    result = await rig.boundary.place(
        stored.intent.intent_id, await rig.clear(stored.intent, effect=effect)
    )
    assert result.status is PlacementStatus.PLACED
    return stored, result.order_id


class _NoStatus(FakeOrder):
    """A live order IBKR lists without a status: one row the decoder cannot read."""

    def row(self) -> dict[str, Any]:
        row = super().row()
        del row["status"]
        return row


def _unreadable_row(order_id: str) -> FakeOrder:
    return _NoStatus(
        order_id=order_id,
        coid=None,
        conid=265598,
        side="SELL",
        quantity=Decimal(1),
        order_type="LMT",
        price=Decimal(100),
        aux_price=None,
        tif="DAY",
    )


# ── the flags ──


@pytest.mark.parametrize("error", _UNCHECKABLE)
def test_a_flag_file_the_system_cannot_stat_counts_as_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: OSError
) -> None:
    set_file_flag(tmp_path, actor="owner", reason="drill", at=START)
    _stat_fails_for(monkeypatch, kill_switch_path(tmp_path), error)
    assert file_flag_on(tmp_path) is True


async def test_a_flag_that_cannot_be_checked_blocks_a_buy(
    order_rig: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_file_flag(order_rig.data_dir, actor="owner", reason="set without configuration", at=START)
    _stat_fails_for(
        monkeypatch, kill_switch_path(order_rig.data_dir), PermissionError(errno.EACCES, "denied")
    )
    stored = await order_rig.intent()
    clearance = await order_rig.clear(stored.intent)
    assert await order_rig.kill.is_on() is True
    with pytest.raises(WriteRefusedError) as refused:
        await order_rig.boundary.place(stored.intent.intent_id, clearance)
    assert refused.value.code == "kill_switch_on"
    assert order_rig.broker.placements == []


async def test_a_flag_file_that_is_a_directory_or_empty_still_counts_as_on(
    order_rig: Any,
) -> None:
    flag = kill_switch_path(order_rig.data_dir)
    flag.mkdir()
    assert await order_rig.kill.is_on() is True
    flag.rmdir()
    flag.write_bytes(b"")
    assert await order_rig.kill.is_on() is True


async def test_the_stat_failure_is_targeted_and_the_switch_is_off_without_flags(
    order_rig: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The positive control for the two tests above: the patch touches only the flag's path,
    and with no flag at all the switch is off and a buy is placed."""
    _stat_fails_for(monkeypatch, order_rig.data_dir / "elsewhere", OSError(errno.EIO, "I/O"))
    assert await order_rig.kill.is_on() is False
    _, order_id = await _place(order_rig, terms())
    assert order_id in order_rig.broker.orders


# ── cancel when everything else fails ──


async def test_a_cancel_reaches_the_broker_with_the_store_and_the_mode_gone(
    order_rig: Any,
) -> None:
    order = order_rig.broker.add_order(side="BUY")

    async def gone(*args: Any, **kwargs: Any) -> Any:
        raise StoreError("the database is gone")

    order_rig.boundary._worker = type("Gone", (), {"run": staticmethod(gone)})()
    order_rig.modes.current = gone
    await order_rig.kill.turn_on(actor="owner", reason="drill")
    with contextlib.suppress(StoreError):  # the read-back may fail; the cancel must not
        await order_rig.boundary.cancel(order.order_id)
    assert order_rig.broker.cancels == [order.order_id]
    assert order_rig.broker.orders[order.order_id].status == "Cancelled"


async def test_the_sweep_cancels_from_a_live_list_that_is_not_a_complete_snapshot(
    order_rig: Any,
) -> None:
    """Completeness matters to the cOID lookup, not to the sweep: what it can see, it cancels."""
    buy = order_rig.broker.add_order(side="BUY")
    order_rig.broker.snapshot = False
    report = await order_rig.kill.sweep(order_rig.client)
    assert report.cancelled == (buy.order_id,)


async def test_one_unreadable_live_order_does_not_stop_the_sweep_cancelling_the_rest(
    order_rig: Any,
) -> None:
    _, buy_id = await _place(order_rig, terms())
    order_rig.broker.orders["9999"] = _unreadable_row("9999")
    await order_rig.kill.turn_on(actor="owner", reason="drill")
    report = await order_rig.kill.sweep(order_rig.client)
    assert buy_id in report.cancelled
    assert order_rig.broker.orders[buy_id].status == "Cancelled"


async def test_the_sweep_loop_survives_a_report_hook_that_fails(order_rig: Any) -> None:
    order_rig.broker.listing_delay = 1  # the first sweep cannot see the order yet
    _, buy_id = await _place(order_rig, terms())
    await order_rig.kill.turn_on(actor="owner", reason="drill")
    order_rig.kill._interval = 0.001
    stop = asyncio.Event()
    reports: list[Any] = []

    def hook(report: Any) -> None:
        reports.append(report)
        if len(reports) == 1:
            raise RuntimeError("the alert channel is down")

    async def stop_when_cancelled() -> None:
        while buy_id not in order_rig.broker.cancels:
            await asyncio.sleep(0.001)
        stop.set()

    watcher = asyncio.create_task(stop_when_cancelled())
    try:
        await asyncio.wait_for(order_rig.kill.run(order_rig.client, stop, on_report=hook), 2)
    finally:
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher
    assert buy_id in order_rig.broker.cancels


# ── classing what the broker lists ──


@pytest.mark.parametrize(
    ("side", "sec_type", "status", "cancelled"),
    [
        ("BUY", "STK", "PreSubmitted", True),
        ("BUY", "STK", "Inactive", True),
        ("BUY", "OPT", "PendingSubmit", True),
        ("SELL", "cash", "Submitted", True),  # a conversion, whatever the case of its type
        ("SELL", "Cash", "PreSubmitted", True),
        ("sell", "STK", "Submitted", False),  # the side's case does not change what it is
        ("SLD", "STK", "Submitted", True),  # a side the decoder cannot read is cancelled
        ("", "STK", "Submitted", True),
        ("BUY", "STK", "FILLED", None),  # final: not a working order at all
        ("BUY", "STK", "cancelled", None),
    ],
)
async def test_the_sweep_classes_odd_broker_spellings_fail_closed(
    order_rig: Any, side: str, sec_type: str, status: str, cancelled: bool | None
) -> None:
    order = order_rig.broker.add_order(side=side, sec_type=sec_type, status=status)
    report = await order_rig.kill.sweep(order_rig.client)
    if cancelled is None:
        assert order.order_id not in report.cancelled + report.kept
    elif cancelled:
        assert report.cancelled == (order.order_id,)
    else:
        assert report.kept == (order.order_id,)


async def test_a_sell_the_store_knows_as_a_conversion_is_cancelled_whatever_the_broker_says(
    order_rig: Any,
) -> None:
    conversion = await order_rig.intent(
        terms(side=OrderSide.SELL, cash="5000", conversion=True, conid=12087792),
        role=IntentRole.CONVERSION,
        reason="fund a trade",
    )
    listed = order_rig.broker.add_order(side="SELL", sec_type="STK", coid=conversion.coid)
    report = await order_rig.kill.sweep(order_rig.client)
    assert report.cancelled == (listed.order_id,)
    assert report.classed_by_broker == ()


@pytest.mark.parametrize(
    ("placed_as", "modify_as"),
    [
        pytest.param(RiskEffect.ADDS, RiskEffect.REDUCES, id="adds-risk sell"),
        pytest.param(RiskEffect.REDUCES, RiskEffect.ADDS, id="adds-risk modify"),
    ],
)
async def test_a_sell_with_any_adds_risk_clearance_is_cancelled(
    order_rig: Any, placed_as: RiskEffect, modify_as: RiskEffect
) -> None:
    """A spent modify may never have reached the broker, so the sweep cannot tell which
    clearance describes the working order: any adds-risk clearance in the intent's history
    cancels it (DECISIONS D48), and a later reducing one never relabels an adds-risk sell."""
    stored, order_id = await _place(
        order_rig,
        terms(side=OrderSide.SELL, quantity="10", stop="95", limit="94"),
        role=IntentRole.PROTECTIVE,
        reason="stop",
        effect=placed_as,
    )
    order_rig.clock.advance(1)
    resized = terms(side=OrderSide.SELL, quantity="5", stop="95", limit="94")
    order_rig.broker.questions = deque([(["o0"], "a warning no policy confirms")])
    result = await order_rig.boundary.modify(
        stored.intent.intent_id,
        order_id=order_id,
        terms=resized,
        clearance=await order_rig.clear(
            stored.intent,
            effect=modify_as,
            action=ClearanceAction.MODIFY,
            order_id=order_id,
            order_terms=resized,
        ),
    )
    assert result.status is PlacementStatus.DECLINED
    assert order_rig.broker.orders[order_id].quantity == Decimal(10)  # it stands as placed
    await order_rig.kill.turn_on(actor="owner", reason="drill")
    report = await order_rig.kill.sweep(order_rig.client)
    assert report.cancelled == (order_id,)
