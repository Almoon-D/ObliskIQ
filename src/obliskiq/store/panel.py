"""The weekly forecast panel's calls to the model (018_scoreboard_remainder), and what they cost
against the one monthly LLM budget. The forecasts are the scoreboard's calls of layer ``panel``.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from .db import Store
from .mappers import read_stamp, stamp_text

type PanelStatus = Literal["ok", "invalid", "failed"]


@dataclass(frozen=True, slots=True)
class PanelCall:
    week: str
    universe: str
    produced_at: datetime
    provider: str
    model_id: str
    prompt_hash: str
    status: PanelStatus
    attempts: int
    asked: int
    valid: int
    error: str | None
    input_tokens: int
    output_tokens: int
    cost_usd: float


def record_panel_call(store: Store, call: PanelCall) -> None:
    with store.transaction():
        store.execute(
            "INSERT INTO panel_calls (week, universe, produced_at, provider, model_id, "
            "prompt_hash, status, attempts, asked, valid, error, input_tokens, output_tokens, "
            "cost_usd) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                call.week,
                call.universe,
                stamp_text(call.produced_at),
                call.provider,
                call.model_id,
                call.prompt_hash,
                call.status,
                call.attempts,
                call.asked,
                call.valid,
                call.error,
                call.input_tokens,
                call.output_tokens,
                call.cost_usd,
            ),
        )


def panel_calls(store: Store, week: str | None = None) -> list[PanelCall]:
    sql = "SELECT * FROM panel_calls"
    params: list[object] = []
    if week is not None:
        sql += " WHERE week = ?"
        params.append(week)
    rows = store.execute(sql + " ORDER BY call_id", params).fetchall()
    return [
        PanelCall(
            week=row["week"],
            universe=row["universe"],
            produced_at=read_stamp(row["produced_at"], "panel_calls", "produced_at"),
            provider=row["provider"],
            model_id=row["model_id"],
            prompt_hash=row["prompt_hash"],
            status=row["status"],
            attempts=row["attempts"],
            asked=row["asked"],
            valid=row["valid"],
            error=row["error"],
            input_tokens=row["input_tokens"],
            output_tokens=row["output_tokens"],
            cost_usd=row["cost_usd"],
        )
        for row in rows
    ]


def panel_spend(store: Store, since: datetime) -> float:
    """What the panel's calls since ``since`` cost."""
    row = store.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) AS spent FROM panel_calls WHERE produced_at >= ?",
        (stamp_text(since),),
    ).fetchone()
    return float(row["spent"])
