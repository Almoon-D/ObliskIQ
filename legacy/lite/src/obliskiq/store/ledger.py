"""Ledger writes, with the gates enforced here rather than upstream.

The skill must never be the only thing standing between a bad trade and the
ledger. The server wraps these functions as MCP tools; the validation lives here
so that a tool, a script and a test all hit the same refusals.

Three rules are structural rather than advisory.

**A thesis is frozen at entry.** ``thesis_text`` and ``invalidation_trigger`` are
written once and never edited — the store enforces it with a trigger, and this
module deliberately offers no function that would try. Changes append to
``reunderwrite_log`` as dated entries, which makes thesis drift detectable
instead of a matter of recollection. A position held for cheap implied vol is not
the position that was bought for a product story.

**An exit needs a reason from the config list.** ``discipline.exit_reasons_allowed``
is the whole set, and ``override`` should be rare enough to be conspicuous. There
is no path that closes a position without one.

**A re-underwrite needs a new dated catalyst.** Falling price is not a catalyst.
This is what stands between the account and averaging down, which is the single
most destructive pattern available to it.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import date
from typing import Any

from ..config import Config
from ..errors import StoreError
from ..logging_setup import get_logger
from ..store.db import Store
from ..util.timeutil import iso_date, parse_date, utc_stamp

log = get_logger("store.ledger")

#: ``option_ledger.event_type``.
#: Premium tied up by an OPEN position. Subtracted from available budget so the
#: sleeve is a shared pool; carries ``penalty_added = 0`` and never moves the
#: penalty balance. It has no release event — a commitment stops counting when
#: its position stops being open.
EVENT_PREMIUM_COMMITTED = "premium_committed"
EVENT_LOSS_DETECTED = "loss_detected"
EVENT_LOSS_CLOSED = "loss_closed"
EVENT_GAIN_REALISED = "gain_realised"
EVENT_QUALIFYING_EQUITY_GAIN = "qualifying_equity_gain"

#: ``position_ledger.option_right``. One letter each, as the CHECK constraint
#: in migration 011 spells them.
RIGHT_CALL = "C"
RIGHT_PUT = "P"


class LedgerError(StoreError):
    """A write was refused because it violates a gate in config.yaml."""


@dataclass(frozen=True)
class Position:
    """A row of ``position_ledger``, as the risk engine reads it."""

    position_id: int
    contract_id: int
    opened_at: str
    entry_price: float
    thesis_text: str
    invalidation_trigger: str
    target: float | None
    time_window_end: str | None
    mandatory_close_date: str | None
    prob_target_at_entry: float | None
    entry_range_pctile: float | None
    reunderwrite_log: list[dict[str, Any]]
    closed_at: str | None
    exit_reason: str | None
    realised_pnl: float | None
    #: Which sleeve this position belongs to. ``None`` means the row predates
    #: migration 011 and nobody recorded it — a third state, and the reason
    #: ``log_exit`` asks rather than assuming. See 011's header.
    is_option: bool | None = None
    contracts: int | None = None
    strike: float | None = None
    option_right: str | None = None
    expiry: str | None = None
    #: The currency ``entry_price`` is quoted in, from the instrument's universe
    #: row. The sleeve is denominated in ``account.base_currency``, so a figure
    #: without its currency cannot be booked against it.
    entry_currency: str | None = None

    @property
    def is_open(self) -> bool:
        return self.closed_at is None

    @property
    def is_call(self) -> bool | None:
        """True for a call, False for a put, ``None`` when not recorded."""
        if self.option_right is None:
            return None
        return self.option_right.upper() == RIGHT_CALL


def open_position(
    store: Store,
    *,
    contract_id: int,
    opened_at: str,
    entry_price: float,
    thesis_text: str,
    invalidation_trigger: str,
    target: float | None = None,
    time_window_end: str | None = None,
    mandatory_close_date: str | None = None,
    prob_target_at_entry: float | None = None,
    entry_range_pctile: float | None = None,
    is_option: bool | None = None,
    contracts: int | None = None,
    strike: float | None = None,
    is_call: bool | None = None,
    expiry: str | None = None,
    entry_currency: str | None = None,
) -> int:
    """Write an entry. Returns the new ``position_id``.

    ``invalidation_trigger`` must be a specific observable — a price level, a
    data release, a reported figure. "If sentiment deteriorates" is not one, and
    a thesis with no falsifier is a position nobody can ever be wrong about.

    **The terms are recorded, not merely gated on.** ``is_option`` decides which
    sleeve a loss lands in, and it used to be re-declared by the caller at exit
    with no way to check the answer: closing an equity position as an option
    booked a penalty against a loss the sleeve never carried. ``contracts``,
    ``strike``, ``is_call``, ``expiry`` and ``entry_currency`` are stored for the
    same reason — every one of them is an input some later gate needs and used to
    have to be supplied again from memory.

    This function enforces LEDGER INTEGRITY only. The mandate — affordability,
    tenor, liquidity, concentration — is enforced by ``server/guards.py`` before
    the call, because it needs the chain and the sleeve, which the store alone
    cannot see.
    """
    if not thesis_text.strip():
        raise LedgerError("thesis_text is required and is frozen at entry")
    if not invalidation_trigger.strip():
        raise LedgerError(
            "invalidation_trigger is required: a specific observable price level, data "
            "release or reported figure. Without one there is no exit except drawdown, "
            "and drawdown is explicitly not an exit."
        )
    if not math.isfinite(entry_price) or entry_price <= 0:
        raise LedgerError(
            f"entry_price must be a finite positive number, got {entry_price!r}. A "
            "NaN or an infinity here would price every later read of this position — "
            "the stops, the zombie test, the portfolio review — off a number that is "
            "not a measurement."
        )
    if contracts is not None and contracts <= 0:
        raise LedgerError(f"contracts must be positive when stated, got {contracts!r}")

    option_right = None if is_call is None else (RIGHT_CALL if is_call else RIGHT_PUT)
    with store.transaction() as connection:
        cursor = connection.execute(
            """
            INSERT INTO position_ledger (
                contract_id, opened_at, entry_price, entry_range_pctile, thesis_text,
                invalidation_trigger, target, time_window_end, mandatory_close_date,
                prob_target_at_entry, is_option, contracts, strike, option_right,
                expiry, entry_currency)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                contract_id,
                opened_at,
                entry_price,
                entry_range_pctile,
                thesis_text,
                invalidation_trigger,
                target,
                time_window_end,
                mandatory_close_date,
                prob_target_at_entry,
                None if is_option is None else int(is_option),
                contracts,
                strike,
                option_right,
                expiry,
                entry_currency,
            ),
        )
    position_id = int(cursor.lastrowid or 0)
    log.info(
        "position opened",
        extra={"position_id": position_id, "contract_id": contract_id},
    )
    return position_id


def close_position(
    store: Store,
    config: Config,
    position_id: int,
    *,
    closed_at: str,
    exit_reason: str,
    realised_pnl: float | None = None,
) -> None:
    """Close a position with a reason from ``discipline.exit_reasons_allowed``."""
    allowed = [str(reason) for reason in config.require_list("discipline.exit_reasons_allowed")]
    if exit_reason not in allowed:
        raise LedgerError(
            f"exit_reason {exit_reason!r} is not in discipline.exit_reasons_allowed "
            f"({allowed}). Price movement alone is never an exit."
        )
    position = get_position(store, position_id)
    if position is None:
        raise LedgerError(f"no position {position_id}")
    if not position.is_open:
        raise LedgerError(
            f"position {position_id} was already closed at {position.closed_at} for "
            f"{position.exit_reason!r}"
        )

    with store.transaction() as connection:
        connection.execute(
            "UPDATE position_ledger SET closed_at = ?, exit_reason = ?, realised_pnl = ? "
            "WHERE position_id = ?",
            (closed_at, exit_reason, realised_pnl, position_id),
        )
    log.info(
        "position closed",
        extra={"position_id": position_id, "exit_reason": exit_reason, "pnl": realised_pnl},
    )


def append_reunderwrite(
    store: Store,
    position_id: int,
    *,
    dated_at: str,
    catalyst: str,
    catalyst_date: str,
    note: str | None = None,
) -> list[dict[str, Any]]:
    """Append a dated re-underwrite. The thesis itself is never edited.

    A re-underwrite must carry a NEW dated catalyst that did not exist at entry.
    Falling price is not a catalyst, and an undated one is a hope with a
    timestamp.
    """
    if not catalyst.strip():
        raise LedgerError("a re-underwrite must name a catalyst")
    parsed_catalyst = parse_date(catalyst_date)
    if parsed_catalyst is None:
        raise LedgerError(
            f"catalyst_date {catalyst_date!r} is not a date. A re-underwrite needs a "
            "NEW DATED catalyst; falling price is not a catalyst."
        )
    position = get_position(store, position_id)
    if position is None:
        raise LedgerError(f"no position {position_id}")

    opened = parse_date(position.opened_at)
    if opened is not None and parsed_catalyst <= opened:
        raise LedgerError(
            f"catalyst_date {catalyst_date} is not after the position was opened on "
            f"{position.opened_at}. A catalyst that existed at entry is part of the "
            "original thesis, not a reason to add to a loser."
        )

    entries = list(position.reunderwrite_log)
    entries.append(
        {
            "dated_at": dated_at,
            "catalyst": catalyst,
            "catalyst_date": iso_date(parsed_catalyst),
            "note": note,
            "written_at": utc_stamp(),
        }
    )
    with store.transaction() as connection:
        connection.execute(
            "UPDATE position_ledger SET reunderwrite_log = ? WHERE position_id = ?",
            (json.dumps(entries), position_id),
        )
    log.info(
        "re-underwrite appended",
        extra={"position_id": position_id, "catalyst_date": iso_date(parsed_catalyst)},
    )
    return entries


def get_position(store: Store, position_id: int) -> Position | None:
    row = store.execute(
        "SELECT * FROM position_ledger WHERE position_id = ?", (position_id,)
    ).fetchone()
    return None if row is None else _to_position(row)


def open_positions(store: Store) -> list[Position]:
    rows = store.execute(
        "SELECT * FROM position_ledger WHERE closed_at IS NULL ORDER BY opened_at"
    ).fetchall()
    return [_to_position(row) for row in rows]


def open_sleeve_positions(store: Store) -> list[Position]:
    """Open positions that count against ``option_sleeve.max_concurrent_positions``.

    The cap is an option-sleeve rule — it bounds fragmentation of the premium
    pool — and it used to count every open row, so a cash equity holding
    consumed a slot in a sleeve it does not belong to.

    A row with ``is_option`` NULL predates migration 011, when the fact was not
    recorded. It is counted, deliberately: the cap's failure directions are not
    symmetric. Counting an equity row costs one refused option entry, which the
    caller can see and argue with; not counting an option row lets the sleeve
    fragment past the limit, silently, which is the thing the cap exists to
    prevent.
    """
    rows = store.execute(
        "SELECT * FROM position_ledger WHERE closed_at IS NULL "
        "AND (is_option IS NULL OR is_option = 1) ORDER BY opened_at"
    ).fetchall()
    return [_to_position(row) for row in rows]


def positions_closed_for(store: Store, reasons: list[str]) -> list[Position]:
    """Closed positions whose exit reason is in ``reasons``.

    The penalty engine uses this for the qualifying-gain set, so the reasons come
    from ``option_sleeve.qualifying_exit_reasons`` and never from a literal.
    """
    if not reasons:
        return []
    placeholders = ", ".join("?" for _ in reasons)
    rows = store.execute(
        # placeholders is a run of "?" generated from len(reasons); the reasons
        # themselves are bound, never interpolated.
        f"SELECT * FROM position_ledger WHERE closed_at IS NOT NULL "
        f"AND exit_reason IN ({placeholders}) ORDER BY closed_at",
        list(reasons),
    ).fetchall()
    return [_to_position(row) for row in rows]


def thesis_is_frozen(store: Store, position_id: int, new_text: str) -> bool:
    """Probe the store's own freeze, for tests and for the self-audit.

    Returns True when the store refused the edit. There is deliberately no
    function here that performs one successfully.
    """
    try:
        with store.transaction() as connection:
            connection.execute(
                "UPDATE position_ledger SET thesis_text = ? WHERE position_id = ?",
                (new_text, position_id),
            )
    except (StoreError, sqlite3.Error):
        return True
    return False


def _to_position(row: sqlite3.Row) -> Position:
    try:
        entries = json.loads(row["reunderwrite_log"] or "[]")
    except (TypeError, ValueError):
        entries = []
    return Position(
        position_id=int(row["position_id"]),
        contract_id=int(row["contract_id"]),
        opened_at=str(row["opened_at"]),
        entry_price=float(row["entry_price"]),
        thesis_text=str(row["thesis_text"]),
        invalidation_trigger=str(row["invalidation_trigger"]),
        target=None if row["target"] is None else float(row["target"]),
        time_window_end=row["time_window_end"],
        mandatory_close_date=row["mandatory_close_date"],
        prob_target_at_entry=(
            None if row["prob_target_at_entry"] is None else float(row["prob_target_at_entry"])
        ),
        entry_range_pctile=(
            None if row["entry_range_pctile"] is None else float(row["entry_range_pctile"])
        ),
        reunderwrite_log=entries if isinstance(entries, list) else [],
        closed_at=row["closed_at"],
        exit_reason=row["exit_reason"],
        realised_pnl=None if row["realised_pnl"] is None else float(row["realised_pnl"]),
        is_option=None if row["is_option"] is None else bool(row["is_option"]),
        contracts=None if row["contracts"] is None else int(row["contracts"]),
        strike=None if row["strike"] is None else float(row["strike"]),
        option_right=row["option_right"],
        expiry=row["expiry"],
        entry_currency=row["entry_currency"],
    )


def has_new_dated_catalyst(position: Position, *, as_of: date) -> bool:
    """Does the re-underwrite log carry a catalyst still ahead of ``as_of``?

    Used by the averaging-down check. A catalyst that has already passed cannot
    justify adding to a loser today: if it passed without the move, that is an
    invalidation, not a reason to buy more.
    """
    opened = parse_date(position.opened_at)
    for entry in position.reunderwrite_log:
        catalyst_date = parse_date(str(entry.get("catalyst_date") or ""))
        if catalyst_date is None or not str(entry.get("catalyst") or "").strip():
            continue
        if opened is not None and catalyst_date <= opened:
            continue
        if catalyst_date >= as_of:
            return True
    return False
