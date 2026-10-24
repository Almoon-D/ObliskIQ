"""The options sleeve in the store (012_engines): its positions, its penalty ledger, its zombie
readings and the would-be entries it logs in shadow.

The ledger is append-only and a position changes once, when it closes; the triggers refuse
anything else. Reads are bounded by date where the question is "as known on": the committed
premium and the penalty balance as of a day count only the events dated by it.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import json
import sqlite3
from collections.abc import Sequence
from datetime import date
from decimal import Decimal, InvalidOperation
from uuid import UUID

from ..domain.enums import MarketDataStatus, OptionRight
from ..domain.options import (
    Conversion,
    LedgerEvent,
    LedgerEventType,
    OptionPosition,
    ShadowEntry,
    ZombieObservation,
)
from ..errors import StoreError
from .db import Store
from .mappers import (
    date_text,
    optional_date_text,
    read_date,
    read_optional_date,
    read_stamp,
    stamp_text,
)

POSITION_COLUMNS = (
    'position_id, underlying_conid, option_conid, expiry, strike, "right", contracts, multiplier, '
    "premium_per_unit, currency, premium_base, fx_to_base, opened_on, close_by, target, "
    "prob_target_at_entry, catalyst_date, intent_id, closed_on, exit_reason"
)
LEDGER_COLUMNS = (
    "event_id, event_on, event_type, position_id, conid, amount, penalty_added, "
    "penalty_balance_after, exit_reason, quote_currency, quote_amount, fx_to_base, note, "
    "recorded_at"
)
SHADOW_COLUMNS = (
    'as_of, underlying_conid, expiry, strike, "right", snapshot_date, option_conid, bid, ask, '
    "mid, iv, delta, multiplier, currency, premium_base, iv_percentile, market_data_status, "
    "untested, activation, computed_at"
)
_SHADOW_COUNT = 20


def _decimal(text: str, table: str, column: str) -> Decimal:
    try:
        return Decimal(text)
    except InvalidOperation:
        raise StoreError(f"{table}.{column} holds {text!r}, which is not a decimal") from None


def _optional_decimal(text: str | None, table: str, column: str) -> Decimal | None:
    return None if text is None else _decimal(text, table, column)


def _optional_text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


# ── positions ──


def open_position(store: Store, position: OptionPosition) -> OptionPosition:
    """Record a new position; returns it numbered."""
    if position.position_id is not None or not position.is_open:
        raise StoreError("a new position is unnumbered and open")
    with store.transaction():
        cursor = store.execute(
            f"INSERT INTO option_positions ({POSITION_COLUMNS}) VALUES "
            "(NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)",
            (
                position.underlying_conid,
                position.option_conid,
                date_text(position.expiry),
                position.strike,
                position.right.value,
                position.contracts,
                position.multiplier,
                str(position.premium_per_unit),
                position.currency,
                str(position.premium_base),
                _optional_text(position.fx_to_base),
                date_text(position.opened_on),
                date_text(position.close_by),
                position.target,
                position.prob_target_at_entry,
                optional_date_text(position.catalyst_date),
                None if position.intent_id is None else str(position.intent_id),
            ),
        )
    number = cursor.lastrowid
    if number is None:  # pragma: no cover - SQLite numbers every INSERT into a rowid table
        raise StoreError("the store did not number the position")
    return position.model_copy(update={"position_id": number})


def close_position(store: Store, position_id: int, *, closed_on: date, exit_reason: str) -> None:
    """Close an open position once, with the reason it closed for."""
    with store.transaction():
        found = get_position(store, position_id)
        if found is None:
            raise StoreError(f"option position {position_id} does not exist")
        if not found.is_open:
            raise StoreError(f"option position {position_id} is already closed")
        if closed_on < found.opened_on:
            raise StoreError(f"option position {position_id} cannot close before it opened")
        store.execute(
            "UPDATE option_positions SET closed_on = ?, exit_reason = ? WHERE position_id = ?",
            (date_text(closed_on), exit_reason, position_id),
        )


def _position_from_row(row: sqlite3.Row) -> OptionPosition:
    table = "option_positions"
    try:
        return OptionPosition(
            position_id=row["position_id"],
            underlying_conid=row["underlying_conid"],
            option_conid=row["option_conid"],
            expiry=read_date(row["expiry"], table, "expiry"),
            strike=row["strike"],
            right=OptionRight(row["right"]),
            contracts=row["contracts"],
            multiplier=row["multiplier"],
            premium_per_unit=_decimal(row["premium_per_unit"], table, "premium_per_unit"),
            currency=row["currency"],
            premium_base=_decimal(row["premium_base"], table, "premium_base"),
            fx_to_base=_optional_decimal(row["fx_to_base"], table, "fx_to_base"),
            opened_on=read_date(row["opened_on"], table, "opened_on"),
            close_by=read_date(row["close_by"], table, "close_by"),
            target=row["target"],
            prob_target_at_entry=row["prob_target_at_entry"],
            catalyst_date=read_optional_date(row["catalyst_date"], table, "catalyst_date"),
            intent_id=None if row["intent_id"] is None else UUID(row["intent_id"]),
            closed_on=read_optional_date(row["closed_on"], table, "closed_on"),
            exit_reason=row["exit_reason"],
        )
    except ValueError as error:
        raise StoreError(
            f"option_positions row {row['position_id']} cannot be read: {error}"
        ) from None


def get_position(store: Store, position_id: int) -> OptionPosition | None:
    row = store.execute(
        f"SELECT {POSITION_COLUMNS} FROM option_positions WHERE position_id = ?", (position_id,)
    ).fetchone()
    return None if row is None else _position_from_row(row)


def open_positions(store: Store, *, as_of: date) -> tuple[OptionPosition, ...]:
    """The positions open at the end of ``as_of``: opened by then and not closed by then."""
    day = date_text(as_of)
    rows = store.execute(
        f"SELECT {POSITION_COLUMNS} FROM option_positions WHERE opened_on <= ? "
        "AND (closed_on IS NULL OR closed_on > ?) ORDER BY position_id",
        (day, day),
    ).fetchall()
    return tuple(_position_from_row(row) for row in rows)


# ── the penalty ledger ──


def append_event(store: Store, event: LedgerEvent) -> LedgerEvent:
    """Append one ledger event; returns it numbered."""
    if event.event_id is not None:
        raise StoreError("a new ledger event is unnumbered")
    conversion = event.conversion
    with store.transaction():
        cursor = store.execute(
            f"INSERT INTO option_ledger ({LEDGER_COLUMNS}) VALUES "
            "(NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                date_text(event.event_on),
                event.event_type.value,
                event.position_id,
                event.conid,
                str(event.amount),
                str(event.penalty_added),
                str(event.penalty_balance_after),
                event.exit_reason,
                None if conversion is None else conversion.currency,
                None if conversion is None else str(conversion.quote_amount),
                None if conversion is None else str(conversion.fx_to_base),
                event.note,
                stamp_text(event.recorded_at),
            ),
        )
    number = cursor.lastrowid
    if number is None:  # pragma: no cover - SQLite numbers every INSERT into a rowid table
        raise StoreError("the store did not number the ledger event")
    return event.model_copy(update={"event_id": number})


def _event_from_row(row: sqlite3.Row) -> LedgerEvent:
    table = "option_ledger"
    try:
        conversion = (
            None
            if row["quote_currency"] is None
            else Conversion(
                currency=row["quote_currency"],
                quote_amount=_decimal(row["quote_amount"], table, "quote_amount"),
                fx_to_base=_decimal(row["fx_to_base"], table, "fx_to_base"),
            )
        )
        return LedgerEvent(
            event_id=row["event_id"],
            event_on=read_date(row["event_on"], table, "event_on"),
            event_type=LedgerEventType(row["event_type"]),
            position_id=row["position_id"],
            conid=row["conid"],
            amount=_decimal(row["amount"], table, "amount"),
            penalty_added=_decimal(row["penalty_added"], table, "penalty_added"),
            penalty_balance_after=_decimal(
                row["penalty_balance_after"], table, "penalty_balance_after"
            ),
            exit_reason=row["exit_reason"],
            conversion=conversion,
            note=row["note"],
            recorded_at=read_stamp(row["recorded_at"], table, "recorded_at"),
        )
    except ValueError as error:
        raise StoreError(f"option_ledger row {row['event_id']} cannot be read: {error}") from None


def ledger_events(store: Store, *, as_of: date) -> tuple[LedgerEvent, ...]:
    """Every event dated on or before ``as_of``, in the order they happened."""
    rows = store.execute(
        f"SELECT {LEDGER_COLUMNS} FROM option_ledger WHERE event_on <= ? "
        "ORDER BY event_on, event_id",
        (date_text(as_of),),
    ).fetchall()
    return tuple(_event_from_row(row) for row in rows)


def position_events(
    store: Store, position_id: int, kinds: Sequence[LedgerEventType]
) -> tuple[LedgerEvent, ...]:
    """A position's events of the given kinds, in order."""
    marks = ", ".join("?" * len(kinds))
    rows = store.execute(
        f"SELECT {LEDGER_COLUMNS} FROM option_ledger WHERE position_id = ? "
        f"AND event_type IN ({marks}) ORDER BY event_on, event_id",
        (position_id, *(kind.value for kind in kinds)),
    ).fetchall()
    return tuple(_event_from_row(row) for row in rows)


# ── zombie readings ──


def record_observation(store: Store, observation: ZombieObservation) -> None:
    """Record one session's review; a re-review of the same session replaces it."""
    with store.transaction():
        store.execute(
            "INSERT INTO zombie_observations (position_id, observed_on, all_conditions_met, "
            "delta, dte, prob_target, prob_ratio_vs_entry, recoverable_pct_of_premium, detail, "
            "recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (position_id, observed_on) DO UPDATE SET "
            "all_conditions_met = excluded.all_conditions_met, delta = excluded.delta, "
            "dte = excluded.dte, prob_target = excluded.prob_target, "
            "prob_ratio_vs_entry = excluded.prob_ratio_vs_entry, "
            "recoverable_pct_of_premium = excluded.recoverable_pct_of_premium, "
            "detail = excluded.detail, recorded_at = excluded.recorded_at",
            (
                observation.position_id,
                date_text(observation.observed_on),
                int(observation.all_conditions_met),
                observation.delta,
                observation.dte,
                observation.prob_target,
                observation.prob_ratio_vs_entry,
                observation.recoverable_pct_of_premium,
                json.dumps(observation.detail, sort_keys=True),
                stamp_text(observation.recorded_at),
            ),
        )


def observations(store: Store, position_id: int, *, as_of: date) -> tuple[tuple[date, bool], ...]:
    """The position's reviews dated on or before ``as_of``, newest first: (session, all met)."""
    rows = store.execute(
        "SELECT observed_on, all_conditions_met FROM zombie_observations "
        "WHERE position_id = ? AND observed_on <= ? ORDER BY observed_on DESC",
        (position_id, date_text(as_of)),
    ).fetchall()
    return tuple(
        (read_date(row[0], "zombie_observations", "observed_on"), bool(row[1])) for row in rows
    )


# ── shadow entries ──


def replace_shadow_entries(store: Store, as_of: date, entries: Sequence[ShadowEntry]) -> int:
    """Replace the would-be entries logged for ``as_of`` with ``entries``; returns how many."""
    if any(entry.as_of != as_of for entry in entries):
        raise StoreError(f"every shadow entry replaced for {as_of} must be dated {as_of}")
    placeholders = ", ".join("?" * _SHADOW_COUNT)
    with store.transaction():
        store.execute("DELETE FROM option_shadow_entries WHERE as_of = ?", (date_text(as_of),))
        store.executemany(
            f"INSERT INTO option_shadow_entries ({SHADOW_COLUMNS}) VALUES ({placeholders})",
            [
                (
                    date_text(entry.as_of),
                    entry.underlying_conid,
                    date_text(entry.expiry),
                    entry.strike,
                    entry.right.value,
                    date_text(entry.snapshot_date),
                    entry.option_conid,
                    entry.bid,
                    entry.ask,
                    entry.mid,
                    entry.iv,
                    entry.delta,
                    entry.multiplier,
                    entry.currency,
                    _optional_text(entry.premium_base),
                    entry.iv_percentile,
                    None if entry.market_data_status is None else entry.market_data_status.value,
                    json.dumps(list(entry.untested)),
                    entry.activation,
                    stamp_text(entry.computed_at),
                )
                for entry in entries
            ],
        )
    return len(entries)


def shadow_entries(store: Store, *, as_of: date) -> tuple[ShadowEntry, ...]:
    """The would-be entries logged for the session ``as_of``."""
    table = "option_shadow_entries"
    rows = store.execute(
        f"SELECT {SHADOW_COLUMNS} FROM option_shadow_entries WHERE as_of = ? "
        'ORDER BY underlying_conid, expiry, "right", strike',
        (date_text(as_of),),
    ).fetchall()
    out: list[ShadowEntry] = []
    for row in rows:
        try:
            out.append(
                ShadowEntry(
                    as_of=read_date(row["as_of"], table, "as_of"),
                    underlying_conid=row["underlying_conid"],
                    expiry=read_date(row["expiry"], table, "expiry"),
                    strike=row["strike"],
                    right=OptionRight(row["right"]),
                    snapshot_date=read_date(row["snapshot_date"], table, "snapshot_date"),
                    option_conid=row["option_conid"],
                    bid=row["bid"],
                    ask=row["ask"],
                    mid=row["mid"],
                    iv=row["iv"],
                    delta=row["delta"],
                    multiplier=row["multiplier"],
                    currency=row["currency"],
                    premium_base=_optional_decimal(row["premium_base"], table, "premium_base"),
                    iv_percentile=row["iv_percentile"],
                    market_data_status=(
                        None
                        if row["market_data_status"] is None
                        else MarketDataStatus(row["market_data_status"])
                    ),
                    untested=tuple(json.loads(row["untested"])),
                    activation=row["activation"],
                    computed_at=read_stamp(row["computed_at"], table, "computed_at"),
                )
            )
        except ValueError as error:
            raise StoreError(f"{table} row {tuple(row)[:5]!r} cannot be read: {error}") from None
    return tuple(out)
