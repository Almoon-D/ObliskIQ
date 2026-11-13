"""The live orders' pre-read on the fake broker: a brokerage session's first live-orders answer is
not a complete snapshot, so the order client reads the list once per session before the read that
counts. The gate, the cOID lookup and the reconciliation all read through it; a pre-read that
fails, or a read after it that is still incomplete, places nothing.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import asyncio
from collections import deque
from typing import Any

import httpx
import pytest

from obliskiq.domain.orders import PretradeClearance, ReconcileStatus
from obliskiq.execution.pretrade import PretradeRefusal, Reason
from obliskiq.ibkr.errors import AuthError, DecodeError, IbkrError, LookupIncompleteError
from obliskiq.ibkr.order_decode import snapshot_flag
from obliskiq.ibkr.orders import PlacementStatus, ReplyVerdict

#: The rate-limit group the live orders are paced by, and the path it names.
GROUP = "live_orders"


@pytest.fixture
def rig(order_rig: Any) -> Any:
    order_rig.ready()
    order_rig.make_execution()
    return order_rig


def first_read_unmarked(rig: Any) -> None:
    """The broker answers as IBKR does after a login: the first live-orders list has no flag."""
    rig.broker.next_snapshots = deque([None])


def failures(rig: Any, kind: str) -> None:
    """The next live-orders read fails for good: refused, unreadable, or down past every retry."""
    answers = {
        "refused": [httpx.Response(403, json={"error": "forbidden"})],
        "not json": [httpx.Response(200, content=b"<html>")],
        "down": [httpx.Response(503, json={"error": "down"})] * (rig.settings.http.retries + 1),
    }
    rig.broker.next_live_answers = deque(answers[kind])


async def _ok() -> None:
    return None


def _confirm(question: Any) -> ReplyVerdict:
    return ReplyVerdict(True, "test confirms")


async def place_directly(rig: Any) -> Any:
    """A placement with a clearance the rig made: only the client's own lookup stands before it."""
    stored = await rig.intent()
    return await rig.client.place_idempotent(
        coid=stored.coid,
        terms=stored.intent.terms,
        clearance=await rig.clear(stored.intent),
        decide=_confirm,
        last_check=_ok,
    )


# ── the gate ──


async def test_first_read_incomplete_and_second_complete_the_gate_proceeds(rig: Any) -> None:
    first_read_unmarked(rig)
    stored = await rig.staged()
    clearance = await rig.gate.clear(stored.intent)
    assert isinstance(clearance, PretradeClearance), clearance.reason
    assert rig.broker.live_order_reads == 2  # the pre-read, then the read that counts
    assert rig.client.first_live_orders_flags == ("absent", "true")
    result = await rig.boundary.place(clearance.intent_id, clearance)
    assert result.status is PlacementStatus.PLACED
    assert len(rig.broker.placements) == 1


async def test_both_reads_incomplete_is_account_unreadable_and_no_placement(rig: Any) -> None:
    rig.broker.snapshot = False
    stored = await rig.staged()
    refusal = await rig.gate.clear(stored.intent)
    assert isinstance(refusal, PretradeRefusal)
    assert Reason.ACCOUNT_UNREADABLE.value in refusal.codes
    assert "complete snapshot" in refusal.reason
    assert rig.broker.live_order_reads == 2
    assert rig.client.first_live_orders_flags == ("false", "false")
    with pytest.raises(LookupIncompleteError):
        await place_directly(rig)
    assert rig.broker.placements == []


@pytest.mark.parametrize("kind", ["refused", "not json", "down"])
async def test_a_pre_read_that_errors_is_account_unreadable(rig: Any, kind: str) -> None:
    failures(rig, kind)
    stored = await rig.staged()
    refusal = await rig.gate.clear(stored.intent)
    assert isinstance(refusal, PretradeRefusal)
    assert Reason.ACCOUNT_UNREADABLE.value in refusal.codes
    assert rig.broker.live_order_reads == 0  # the read that counts was never taken
    assert rig.client.first_live_orders_flags == ()
    assert rig.broker.placements == []
    # A failed pre-read is not counted: the next read takes it again, then the gate clears.
    first_read_unmarked(rig)
    assert isinstance(await rig.gate.clear(stored.intent), PretradeClearance)
    assert rig.broker.live_order_reads == 2


@pytest.mark.parametrize("kind", ["refused", "not json", "down"])
async def test_a_pre_read_that_errors_places_nothing(rig: Any, kind: str) -> None:
    failures(rig, kind)
    with pytest.raises(IbkrError):
        await place_directly(rig)
    assert rig.broker.live_order_reads == 0
    assert rig.broker.trade_reads == 0
    assert rig.broker.placements == []


async def test_a_failed_pre_read_spends_no_clearance(rig: Any) -> None:
    stored = await rig.intent()
    clearance = await rig.clear(stored.intent)
    failures(rig, "refused")
    with pytest.raises(AuthError):
        await rig.client.place_idempotent(
            coid=stored.coid,
            terms=stored.intent.terms,
            clearance=clearance,
            decide=_confirm,
            last_check=_ok,
        )
    assert rig.broker.placements == []
    result = await rig.client.place_idempotent(  # the same clearance still places
        coid=stored.coid,
        terms=stored.intent.terms,
        clearance=clearance,
        decide=_confirm,
        last_check=_ok,
    )
    assert result.status is PlacementStatus.PLACED


# ── once per brokerage session ──


async def test_the_pre_read_happens_once_per_session_not_once_per_order(rig: Any) -> None:
    first_read_unmarked(rig)
    for count in (1, 2, 3):
        result = await place_directly(rig)
        assert result.status is PlacementStatus.PLACED
        assert rig.broker.live_order_reads == 1 + count  # one pre-read, one lookup an order
    assert len(rig.broker.placements) == 3
    assert rig.client.first_live_orders_flags == ("absent", "true")


async def test_readers_that_start_together_share_one_pre_read(rig: Any) -> None:
    first_read_unmarked(rig)
    lists = await asyncio.gather(*(rig.client.live_orders() for _ in range(3)))
    assert all(found.complete for found in lists)
    assert rig.broker.live_order_reads == 4


async def test_the_pre_read_is_paced_by_the_live_orders_group(rig: Any, fake_clock: Any) -> None:
    limiter = rig.session.limiter
    assert limiter.group_for("iserver/account/orders") == GROUP
    window = next(g for g in rig.settings.ibkr.ratelimit.groups if g.name == GROUP).windows[0]
    before = fake_clock.monotonic()
    await rig.client.live_orders()
    assert limiter.sent(GROUP) == 2
    # The read that counts waited for the group's window after the pre-read.
    assert fake_clock.monotonic() - before >= window.per_seconds


async def test_a_repaired_session_is_pre_read_again(rig: Any) -> None:
    await rig.client.live_orders()
    await rig.client.live_orders()
    assert rig.broker.live_order_reads == 3
    await rig.session.rearm()  # the session dropped and was re-opened
    first_read_unmarked(rig)
    found = await rig.client.live_orders()
    assert found.complete
    assert rig.broker.live_order_reads == 5
    assert rig.client.first_live_orders_flags == ("absent", "true")


async def test_a_pre_read_a_repair_overlapped_is_not_counted(rig: Any) -> None:
    # The pre-read meets a dropped session: the request re-arms it and is repeated. Which
    # session answered is not known to the client, so the next read pre-reads once more.
    rig.broker.next_live_answers = deque([httpx.Response(401, json={"error": "unauthorized"})])
    generation = rig.session.generation
    await rig.client.live_orders()
    assert rig.session.generation == generation + 1
    assert rig.broker.live_order_reads == 2
    assert rig.client.first_live_orders_flags == ()
    await rig.client.live_orders()
    assert rig.broker.live_order_reads == 4
    await rig.client.live_orders()
    assert rig.broker.live_order_reads == 5


# ── the cOID lookup and the reconciliation take the same path ──


async def test_the_coid_lookup_takes_the_same_path(rig: Any) -> None:
    first_read_unmarked(rig)
    lookup = await rig.client.find("no-such-coid")
    assert not lookup.found
    assert rig.broker.live_order_reads == 2
    assert rig.broker.trade_reads == 1


async def test_the_coid_lookup_fails_closed(rig: Any) -> None:
    rig.broker.snapshot = False
    with pytest.raises(LookupIncompleteError):
        await rig.client.find("no-such-coid")
    rig.broker.snapshot = True
    failures(rig, "not json")
    await rig.session.rearm()
    with pytest.raises(DecodeError):
        await rig.client.find("no-such-coid")
    assert rig.broker.trade_reads == 0


async def test_reconcile_takes_the_same_path(rig: Any) -> None:
    first_read_unmarked(rig)
    run = await rig.reconciler.run()
    assert run.status is ReconcileStatus.GREEN
    assert rig.broker.live_order_reads == 2
    await rig.reconciler.run()
    assert rig.broker.live_order_reads == 3  # no second pre-read in the same session


@pytest.mark.parametrize("kind", ["both incomplete", "refused", "not json", "down"])
async def test_reconcile_fails_closed(rig: Any, kind: str) -> None:
    if kind == "both incomplete":
        rig.broker.snapshot = False
    else:
        failures(rig, kind)
    run = await rig.reconciler.run()
    assert run.status is ReconcileStatus.INCOMPLETE
    assert [finding.kind for finding in run.findings] == ["broker_unreadable"]


# ── the kill switch ──


async def test_the_sweep_reads_after_the_pre_read(rig: Any) -> None:
    buy = rig.broker.add_order(side="BUY")
    first_read_unmarked(rig)
    report = await rig.kill.sweep(rig.client)
    assert report.cancelled == (buy.order_id,)
    assert rig.broker.live_order_reads == 2


async def test_a_failed_pre_read_does_not_stop_the_sweep(rig: Any) -> None:
    buy = rig.broker.add_order(side="BUY")
    failures(rig, "refused")
    report = await rig.kill.sweep(rig.client)
    assert report.error is None
    assert report.cancelled == (buy.order_id,)
    assert rig.broker.live_order_reads == 1  # the sweep's own read
    await rig.kill.sweep(rig.client)
    assert rig.broker.live_order_reads == 3  # the next pass takes the pre-read again


# ── the flag ──


@pytest.mark.parametrize(
    ("body", "flag"),
    [
        ({"orders": [], "snapshot": True}, "true"),
        ({"orders": [], "snapshot": False}, "false"),
        ({"orders": []}, "absent"),
        ([], "absent"),
        ({"orders": [], "snapshot": "true"}, "unreadable"),
        ({"orders": [], "snapshot": 1}, "unreadable"),
    ],
)
def test_the_snapshot_flag_is_worded_as_the_answer_carries_it(body: Any, flag: str) -> None:
    assert snapshot_flag(body) == flag
