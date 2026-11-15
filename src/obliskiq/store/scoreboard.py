"""The scoreboard's records (017_scoreboard): calls, layer events, a layer's months beside its
twin, monitoring looks, allocation events, weight sets and drills.

Every table is append-only; a month and a look are written once (a second write of the same key
is refused by the store and reported here as ``False``). The current value of anything is its
latest row.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

from ..domain.orders import ExecutionMode
from .db import Store
from .mappers import read_stamp, stamp_text

type Direction = Literal["up", "down"]
type LayerEventKind = Literal["active", "shadow", "freeze", "unfreeze"]
type Boundary = Literal["gap", "twin"]
type DrillKind = Literal["breaker", "kill", "cash_flow"]

DRILL_KINDS: tuple[DrillKind, ...] = ("breaker", "kill", "cash_flow")


@dataclass(frozen=True, slots=True)
class Call:
    """A layer's call on an instrument, stamped when made (``call_id`` is ``None`` until it is
    recorded)."""

    layer: str
    conid: int
    direction: Direction
    called_at: datetime
    detail: str
    probability: float | None = None
    base_rate: float | None = None
    expected_excess: float | None = None
    baseline: float | None = None
    call_id: int | None = None


@dataclass(frozen=True, slots=True)
class LayerEvent:
    at: datetime
    layer: str
    event: LayerEventKind
    actor: str
    detail: str


@dataclass(frozen=True, slots=True)
class LayerMonth:
    """A layer's month (``YYYY-MM``): its live, twin and reference log returns; ``None`` where
    that month was not measured."""

    layer: str
    month: str
    live: float | None
    twin: float | None
    reference: float | None
    recorded_at: datetime


@dataclass(frozen=True, slots=True)
class Look:
    """One monthly look at one boundary: ``statistic`` and ``critical`` are ``None`` when the
    look was not evaluable."""

    layer: str
    month: str
    boundary: Boundary
    statistic: float | None
    critical: float | None
    spent: float
    breached: bool
    detail: str
    at: datetime

    @property
    def evaluable(self) -> bool:
        return self.statistic is not None


@dataclass(frozen=True, slots=True)
class AllocationEvent:
    at: datetime
    layer: str
    share: Decimal
    actor: str
    reason: str


@dataclass(frozen=True, slots=True)
class WeightSet:
    layer: str
    sha256: str
    weights: str
    at: datetime


@dataclass(frozen=True, slots=True)
class Drill:
    at: datetime
    kind: DrillKind
    mode: ExecutionMode
    passed: bool
    actor: str
    detail: str


# ── calls ──


def record_call(store: Store, call: Call) -> Call:
    with store.transaction():
        cursor = store.execute(
            "INSERT INTO scoreboard_calls (layer, conid, direction, called_at, probability, "
            "base_rate, expected_excess, baseline, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                call.layer,
                call.conid,
                call.direction,
                stamp_text(call.called_at),
                call.probability,
                call.base_rate,
                call.expected_excess,
                call.baseline,
                call.detail,
            ),
        )
    return Call(
        layer=call.layer,
        conid=call.conid,
        direction=call.direction,
        called_at=call.called_at,
        detail=call.detail,
        probability=call.probability,
        base_rate=call.base_rate,
        expected_excess=call.expected_excess,
        baseline=call.baseline,
        call_id=cursor.lastrowid,
    )


def _call(row: sqlite3.Row) -> Call:
    return Call(
        layer=row["layer"],
        conid=row["conid"],
        direction=row["direction"],
        called_at=read_stamp(row["called_at"], "scoreboard_calls", "called_at"),
        detail=row["detail"],
        probability=row["probability"],
        base_rate=row["base_rate"],
        expected_excess=row["expected_excess"],
        baseline=row["baseline"],
        call_id=row["call_id"],
    )


def calls(store: Store, *, as_of: datetime, layer: str | None = None) -> list[Call]:
    """The calls made at or before ``as_of`` (of one layer, or every layer), oldest first."""
    sql = "SELECT * FROM scoreboard_calls WHERE called_at <= ?"
    params: list[object] = [stamp_text(as_of)]
    if layer is not None:
        sql += " AND layer = ?"
        params.append(layer)
    rows = store.execute(sql + " ORDER BY called_at, call_id", params).fetchall()
    return [_call(row) for row in rows]


def layers_called(store: Store) -> list[str]:
    rows = store.execute("SELECT DISTINCT layer FROM scoreboard_calls ORDER BY layer").fetchall()
    return [row[0] for row in rows]


# ── layer events ──


def record_layer_event(store: Store, event: LayerEvent) -> None:
    with store.transaction():
        store.execute(
            "INSERT INTO layer_events (at, layer, event, actor, detail) VALUES (?, ?, ?, ?, ?)",
            (stamp_text(event.at), event.layer, event.event, event.actor, event.detail),
        )


def layer_events(store: Store, layer: str | None = None) -> list[LayerEvent]:
    sql = "SELECT at, layer, event, actor, detail FROM layer_events"
    params: list[object] = []
    if layer is not None:
        sql += " WHERE layer = ?"
        params.append(layer)
    rows = store.execute(sql + " ORDER BY event_id", params).fetchall()
    return [
        LayerEvent(
            at=read_stamp(row["at"], "layer_events", "at"),
            layer=row["layer"],
            event=row["event"],
            actor=row["actor"],
            detail=row["detail"],
        )
        for row in rows
    ]


# ── a layer's months ──


def record_month(store: Store, month: LayerMonth) -> bool:
    """Record a layer's month; ``False`` when that month is already recorded."""
    with store.transaction():
        cursor = store.execute(
            "INSERT INTO layer_months (layer, month, live, twin, reference, recorded_at) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (layer, month) DO NOTHING",
            (
                month.layer,
                month.month,
                month.live,
                month.twin,
                month.reference,
                stamp_text(month.recorded_at),
            ),
        )
        return cursor.rowcount > 0


def layer_months(store: Store, layer: str) -> list[LayerMonth]:
    rows = store.execute(
        "SELECT * FROM layer_months WHERE layer = ? ORDER BY month", (layer,)
    ).fetchall()
    return [
        LayerMonth(
            layer=row["layer"],
            month=row["month"],
            live=row["live"],
            twin=row["twin"],
            reference=row["reference"],
            recorded_at=read_stamp(row["recorded_at"], "layer_months", "recorded_at"),
        )
        for row in rows
    ]


# ── looks ──


def record_look(store: Store, look: Look) -> bool:
    """Record a look; ``False`` when that layer's month already has a look at that boundary."""
    with store.transaction():
        cursor = store.execute(
            "INSERT INTO monitor_looks (layer, month, boundary, statistic, critical, spent, "
            "breached, evaluable, detail, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (layer, month, boundary) DO NOTHING",
            (
                look.layer,
                look.month,
                look.boundary,
                look.statistic,
                look.critical,
                look.spent,
                int(look.breached),
                int(look.evaluable),
                look.detail,
                stamp_text(look.at),
            ),
        )
        return cursor.rowcount > 0


def looks(store: Store, layer: str | None = None) -> list[Look]:
    sql = "SELECT * FROM monitor_looks"
    params: list[object] = []
    if layer is not None:
        sql += " WHERE layer = ?"
        params.append(layer)
    rows = store.execute(sql + " ORDER BY layer, month, boundary", params).fetchall()
    return [
        Look(
            layer=row["layer"],
            month=row["month"],
            boundary=row["boundary"],
            statistic=row["statistic"],
            critical=row["critical"],
            spent=row["spent"],
            breached=bool(row["breached"]),
            detail=row["detail"],
            at=read_stamp(row["at"], "monitor_looks", "at"),
        )
        for row in rows
    ]


# ── allocation ──


def record_allocation(store: Store, event: AllocationEvent) -> None:
    with store.transaction():
        store.execute(
            "INSERT INTO allocation_events (at, layer, share, actor, reason) VALUES "
            "(?, ?, ?, ?, ?)",
            (stamp_text(event.at), event.layer, str(event.share), event.actor, event.reason),
        )


def allocation_events(store: Store, layer: str | None = None) -> list[AllocationEvent]:
    sql = "SELECT at, layer, share, actor, reason FROM allocation_events"
    params: list[object] = []
    if layer is not None:
        sql += " WHERE layer = ?"
        params.append(layer)
    rows = store.execute(sql + " ORDER BY event_id", params).fetchall()
    return [
        AllocationEvent(
            at=read_stamp(row["at"], "allocation_events", "at"),
            layer=row["layer"],
            share=Decimal(row["share"]),
            actor=row["actor"],
            reason=row["reason"],
        )
        for row in rows
    ]


def current_shares(store: Store) -> dict[str, Decimal]:
    """Each layer's latest share of investable capital on the live account."""
    return {event.layer: event.share for event in allocation_events(store)}


# ── weights ──


def record_weights(store: Store, weights: WeightSet) -> bool:
    """Record a weight set; ``False`` when the same set is already recorded for the layer."""
    with store.transaction():
        cursor = store.execute(
            "INSERT INTO weight_sets (layer, sha256, weights, at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (layer, sha256) DO NOTHING",
            (weights.layer, weights.sha256, weights.weights, stamp_text(weights.at)),
        )
        return cursor.rowcount > 0


def latest_weights(store: Store, layer: str) -> WeightSet | None:
    row = store.execute(
        "SELECT layer, sha256, weights, at FROM weight_sets WHERE layer = ? "
        "ORDER BY at DESC LIMIT 1",
        (layer,),
    ).fetchone()
    if row is None:
        return None
    return WeightSet(
        layer=row["layer"],
        sha256=row["sha256"],
        weights=row["weights"],
        at=read_stamp(row["at"], "weight_sets", "at"),
    )


# ── drills ──


def record_drill(store: Store, drill: Drill) -> None:
    with store.transaction():
        store.execute(
            "INSERT INTO drills (at, kind, mode, passed, actor, detail) VALUES (?, ?, ?, ?, ?, ?)",
            (
                stamp_text(drill.at),
                drill.kind,
                drill.mode.value,
                int(drill.passed),
                drill.actor,
                drill.detail,
            ),
        )


def drills(store: Store) -> list[Drill]:
    rows = store.execute(
        "SELECT at, kind, mode, passed, actor, detail FROM drills ORDER BY drill_id"
    ).fetchall()
    return [
        Drill(
            at=read_stamp(row["at"], "drills", "at"),
            kind=row["kind"],
            mode=ExecutionMode(row["mode"]),
            passed=bool(row["passed"]),
            actor=row["actor"],
            detail=row["detail"],
        )
        for row in rows
    ]
