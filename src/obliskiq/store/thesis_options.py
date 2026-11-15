"""The option a thesis is held as, and its twin, in the store (016_thesis_options).

A row is written when the thesis is approved as an option and never deleted: its terms are fixed,
its sleeve position is set once (the block's fills opened it) and its twin closes once. The pair
grades (H8's forward record) are read from these rows and the sleeve's own ledger: nothing about
a pair is stored twice.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import sqlite3
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from ..domain.enums import OptionRight
from ..domain.options import LedgerEventType, PairGrade, ThesisOption
from ..errors import StoreError
from . import options as options_repo
from .db import Store
from .mappers import date_text, read_date, read_optional_stamp, stamp_text

_TABLE = "thesis_options"
_COLUMNS = (
    "thesis_id, option_conid, underlying_conid, symbol, exchange, description, currency, expiry, "
    'strike, "right", multiplier, contracts, close_by, prob_target_at_entry, underlying_entry, '
    "twin_size_base, position_id, twin_exit, twin_exit_reason, twin_closed_at"
)
ZERO = Decimal(0)


def _decimal(text: str, column: str) -> Decimal:
    try:
        return Decimal(text)
    except InvalidOperation:
        raise StoreError(f"{_TABLE}.{column} holds {text!r}, which is not a decimal") from None


def _from_row(row: sqlite3.Row) -> ThesisOption:
    try:
        return ThesisOption(
            thesis_id=row["thesis_id"],
            option_conid=row["option_conid"],
            underlying_conid=row["underlying_conid"],
            symbol=row["symbol"],
            exchange=row["exchange"],
            description=row["description"],
            currency=row["currency"],
            expiry=read_date(row["expiry"], _TABLE, "expiry"),
            strike=row["strike"],
            right=OptionRight(row["right"]),
            multiplier=row["multiplier"],
            contracts=row["contracts"],
            close_by=read_date(row["close_by"], _TABLE, "close_by"),
            prob_target_at_entry=row["prob_target_at_entry"],
            underlying_entry=_decimal(row["underlying_entry"], "underlying_entry"),
            twin_size_base=_decimal(row["twin_size_base"], "twin_size_base"),
            position_id=row["position_id"],
            twin_exit=(
                None if row["twin_exit"] is None else _decimal(row["twin_exit"], "twin_exit")
            ),
            twin_exit_reason=row["twin_exit_reason"],
            twin_closed_at=read_optional_stamp(row["twin_closed_at"], _TABLE, "twin_closed_at"),
        )
    except ValueError as error:
        raise StoreError(f"{_TABLE} row {row['thesis_id']} cannot be read: {error}") from None


def record(store: Store, chosen: ThesisOption) -> None:
    """Record the option chosen for a thesis, at its approval."""
    if chosen.position_id is not None or not chosen.twin_open:
        raise StoreError("a chosen option has no position yet and its twin is open")
    with store.transaction():
        store.execute(
            f"INSERT INTO {_TABLE} ({_COLUMNS}) VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL)",
            (
                chosen.thesis_id,
                chosen.option_conid,
                chosen.underlying_conid,
                chosen.symbol,
                chosen.exchange,
                chosen.description,
                chosen.currency,
                date_text(chosen.expiry),
                chosen.strike,
                chosen.right.value,
                chosen.multiplier,
                chosen.contracts,
                date_text(chosen.close_by),
                chosen.prob_target_at_entry,
                format(chosen.underlying_entry, "f"),
                format(chosen.twin_size_base, "f"),
            ),
        )


def get(store: Store, thesis_id: int) -> ThesisOption | None:
    row = store.execute(
        f"SELECT {_COLUMNS} FROM {_TABLE} WHERE thesis_id = ?", (thesis_id,)
    ).fetchone()
    return None if row is None else _from_row(row)


def every(store: Store) -> list[ThesisOption]:
    """Every option a thesis was approved as, in order of its thesis."""
    rows = store.execute(f"SELECT {_COLUMNS} FROM {_TABLE} ORDER BY thesis_id").fetchall()
    return [_from_row(row) for row in rows]


def set_position(store: Store, thesis_id: int, position_id: int) -> None:
    """Name the sleeve position the block's fills opened."""
    with store.transaction():
        store.execute(
            f"UPDATE {_TABLE} SET position_id = ? WHERE thesis_id = ?", (position_id, thesis_id)
        )


def close_twin(store: Store, thesis_id: int, *, price: Decimal, reason: str, at: datetime) -> None:
    """Close a twin at the underlying's ``price``, for the thesis exit it reached."""
    with store.transaction():
        store.execute(
            f"UPDATE {_TABLE} SET twin_exit = ?, twin_exit_reason = ?, twin_closed_at = ? "
            "WHERE thesis_id = ?",
            (format(price, "f"), reason, stamp_text(at), thesis_id),
        )


def open_twins(store: Store) -> list[ThesisOption]:
    """The twins still running: their option was bought, and no thesis exit has ended them."""
    rows = store.execute(
        f"SELECT {_COLUMNS} FROM {_TABLE} WHERE position_id IS NOT NULL AND twin_exit IS NULL "
        "ORDER BY thesis_id"
    ).fetchall()
    return [_from_row(row) for row in rows]


def pairs(store: Store, *, as_of: date) -> list[PairGrade]:
    """Every option closed by ``as_of`` beside its twin, in the order the options closed. The
    option's result is the sleeve ledger's: its realised loss or gain at the close."""
    found: list[tuple[date, PairGrade]] = []
    for chosen in every(store):
        if chosen.position_id is None:
            continue
        position = options_repo.get_position(store, chosen.position_id)
        if position is None or position.closed_on is None or position.closed_on > as_of:
            continue
        result = ZERO
        for event in options_repo.position_events(
            store, chosen.position_id, (LedgerEventType.LOSS_CLOSED, LedgerEventType.OPTION_GAIN)
        ):
            gain = event.event_type is LedgerEventType.OPTION_GAIN
            result = event.amount if gain else -event.amount
        payoff = chosen.twin_payoff
        closed_by = chosen.twin_closed_at
        twin_known = payoff is not None and closed_by is not None and closed_by.date() <= as_of
        assert position.exit_reason is not None  # a closed position carries its reason
        found.append(
            (
                position.closed_on,
                PairGrade(
                    thesis_id=chosen.thesis_id,
                    right=chosen.right,
                    exit_reason=position.exit_reason,
                    premium_base=position.premium_base,
                    option_pnl=result,
                    twin_size_base=chosen.twin_size_base,
                    twin_pnl=chosen.twin_size_base * payoff
                    if twin_known and payoff is not None
                    else None,
                ),
            )
        )
    return [pair for _, pair in sorted(found, key=lambda item: (item[0], item[1].thesis_id))]


def option_multiplier(store: Store, option_conid: int) -> Decimal | None:
    """The contract multiplier of an option a thesis was held as; ``None`` for any other
    instrument."""
    row = store.execute(
        "SELECT multiplier FROM thesis_options WHERE option_conid = ? LIMIT 1", (option_conid,)
    ).fetchone()
    return None if row is None else Decimal(str(row[0]))
