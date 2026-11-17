"""The stock screen's records: the SEC's listing snapshots and the names that left them, each
company's SIC, the XBRL frame values as read, and Yahoo's daily bars (``019_stocks``).

Every read of what was collected takes a keyword-only ``as_of`` and sees the rows whose
``known_at`` falls before its end. The collector's own bookkeeping (what is held already, which
companies still lack a SIC) reads the whole store: it decides what to ask for next, never what a
view shows.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import sqlite3
from collections.abc import Callable, Iterable, Sequence
from datetime import date, datetime
from typing import NamedTuple

from ..domain.stocks import LeftListing, ListingSnapshot, StockBar, StockFact, StockListing
from ..errors import StoreError
from .db import Store
from .mappers import cutoff_text, date_text, read_date, read_stamp, stamp_text

_FACT_COLUMNS = "cik, input, period, period_end, value, accession, known_at"
_BAR_COLUMNS = "symbol, session, close, volume, change, known_at"


class HeldBar(NamedTuple):
    """A stored session as the screen reads it in bulk (no validation per row)."""

    symbol: str
    session: date
    close: float
    volume: float | None
    change: float | None


def _read[T](table: str, row: sqlite3.Row, make: Callable[[], T]) -> T:
    try:
        return make()
    except ValueError as error:
        raise StoreError(f"{table} row {tuple(row)[:3]!r} cannot be read: {error}") from None


# ── listings ──


def _snapshot(row: sqlite3.Row) -> ListingSnapshot:
    return _read(
        "stock_snapshots",
        row,
        lambda: ListingSnapshot(
            snapshot_id=row["snapshot_id"],
            known_at=read_stamp(row["known_at"], "stock_snapshots", "known_at"),
            listed=row["listed"],
            content_sha256=row["content_sha256"],
        ),
    )


def latest_snapshot_held(store: Store) -> ListingSnapshot | None:
    """The newest snapshot in the store (the collector's comparison base)."""
    row = store.execute(
        "SELECT snapshot_id, known_at, listed, content_sha256 FROM stock_snapshots "
        "ORDER BY snapshot_id DESC LIMIT 1"
    ).fetchone()
    return None if row is None else _snapshot(row)


def save_snapshot(
    store: Store, listings: Sequence[StockListing], *, known_at: datetime, content_sha256: str
) -> tuple[ListingSnapshot, tuple[LeftListing, ...]]:
    """Keep a snapshot whole, the words of each name not held yet, and every name the previous
    snapshot listed that this one does not, as left_listing."""
    if not listings:
        raise StoreError("a snapshot lists at least one name")
    with store.transaction():
        previous = latest_snapshot_held(store)
        cursor = store.execute(
            "INSERT INTO stock_snapshots (known_at, listed, content_sha256) VALUES (?, ?, ?)",
            (stamp_text(known_at), len(listings), content_sha256),
        )
        snapshot_id = cursor.lastrowid
        if snapshot_id is None:  # pragma: no cover - sqlite always assigns one
            raise StoreError("the snapshot was not given an id")
        store.executemany(
            "INSERT INTO stock_members (snapshot_id, ticker, cik) VALUES (?, ?, ?) "
            "ON CONFLICT DO NOTHING",
            [(snapshot_id, listing.ticker, listing.cik) for listing in listings],
        )
        store.executemany(
            "INSERT INTO stock_names (ticker, cik, name, exchange, venue, known_at) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
            [
                (s.ticker, s.cik, s.name, s.exchange, s.venue, stamp_text(known_at))
                for s in listings
            ],
        )
        left: list[LeftListing] = []
        if previous is not None:
            now = {(listing.ticker, listing.cik) for listing in listings}
            before = store.execute(
                "SELECT ticker, cik FROM stock_members WHERE snapshot_id = ? ORDER BY ticker, cik",
                (previous.snapshot_id,),
            ).fetchall()
            gone = [(row["ticker"], row["cik"]) for row in before if (row[0], row[1]) not in now]
            store.executemany(
                "INSERT INTO stock_left (ticker, cik, last_snapshot, left_snapshot) "
                "VALUES (?, ?, ?, ?)",
                [(ticker, cik, previous.snapshot_id, snapshot_id) for ticker, cik in gone],
            )
            left = [
                LeftListing(
                    cik=cik, ticker=ticker, last_seen_at=previous.known_at, left_at=known_at
                )
                for ticker, cik in gone
            ]
    snapshot = ListingSnapshot(
        snapshot_id=snapshot_id,
        known_at=known_at,
        listed=len(listings),
        content_sha256=content_sha256,
    )
    return snapshot, tuple(left)


def snapshot_as_of(store: Store, *, as_of: date, first: bool = False) -> ListingSnapshot | None:
    """The newest snapshot collected by the end of ``as_of`` (the oldest with ``first``)."""
    order = "ASC" if first else "DESC"
    row = store.execute(
        "SELECT snapshot_id, known_at, listed, content_sha256 FROM stock_snapshots "
        f"WHERE known_at < ? ORDER BY snapshot_id {order} LIMIT 1",
        (cutoff_text(as_of),),
    ).fetchone()
    return None if row is None else _snapshot(row)


def listed(store: Store, snapshot: ListingSnapshot) -> tuple[StockListing, ...]:
    """Every name ``snapshot`` kept, by ticker."""
    rows = store.execute(
        "SELECT m.ticker, m.cik, n.name, n.exchange, n.venue FROM stock_members m "
        "JOIN stock_names n ON n.ticker = m.ticker AND n.cik = m.cik "
        "WHERE m.snapshot_id = ? ORDER BY m.ticker, m.cik",
        (snapshot.snapshot_id,),
    ).fetchall()
    return tuple(_listing(row) for row in rows)


def _listing(row: sqlite3.Row) -> StockListing:
    return _read(
        "stock_members",
        row,
        lambda: StockListing(
            cik=row["cik"],
            ticker=row["ticker"],
            name=row["name"],
            exchange=row["exchange"],
            venue=row["venue"],
        ),
    )


def left_listings(store: Store, *, as_of: date) -> tuple[LeftListing, ...]:
    """Every name that left the listings by the end of ``as_of``, oldest first."""
    rows = store.execute(
        "SELECT l.ticker, l.cik, a.known_at AS last_seen_at, b.known_at AS left_at "
        "FROM stock_left l JOIN stock_snapshots a ON a.snapshot_id = l.last_snapshot "
        "JOIN stock_snapshots b ON b.snapshot_id = l.left_snapshot "
        "WHERE b.known_at < ? ORDER BY b.snapshot_id, l.ticker",
        (cutoff_text(as_of),),
    ).fetchall()
    return tuple(_left(row) for row in rows)


def _left(row: sqlite3.Row) -> LeftListing:
    return _read(
        "stock_left",
        row,
        lambda: LeftListing(
            cik=row["cik"],
            ticker=row["ticker"],
            last_seen_at=read_stamp(row["last_seen_at"], "stock_left", "last_seen_at"),
            left_at=read_stamp(row["left_at"], "stock_left", "left_at"),
        ),
    )


def symbol_ciks(store: Store, *, as_of: date) -> dict[str, int]:
    """Every ticker ever listed by the end of ``as_of``, with its company (current and gone)."""
    rows = store.execute(
        "SELECT ticker, cik FROM stock_names WHERE known_at < ? ORDER BY ticker, known_at",
        (cutoff_text(as_of),),
    ).fetchall()
    return {row["ticker"]: row["cik"] for row in rows}


# ── companies ──


def ciks_without_sic(store: Store, *, limit: int) -> tuple[int, ...]:
    """Companies of the newest snapshot whose submissions were never read, at most ``limit``."""
    rows = store.execute(
        "SELECT DISTINCT m.cik FROM stock_members m LEFT JOIN stock_companies c ON c.cik = m.cik "
        "WHERE m.snapshot_id = (SELECT MAX(snapshot_id) FROM stock_snapshots) "
        "AND c.cik IS NULL ORDER BY m.cik LIMIT ?",
        (limit,),
    ).fetchall()
    return tuple(row["cik"] for row in rows)


def save_company(store: Store, cik: int, sic: int | None, *, known_at: datetime) -> None:
    with store.transaction():
        store.execute(
            "INSERT INTO stock_companies (cik, sic, known_at) VALUES (?, ?, ?) "
            "ON CONFLICT (cik) DO NOTHING",
            (cik, sic, stamp_text(known_at)),
        )


def company_sics(store: Store, *, as_of: date) -> dict[int, int | None]:
    """Each company's SIC known by the end of ``as_of`` (``None``: the SEC states none)."""
    rows = store.execute(
        "SELECT cik, sic FROM stock_companies WHERE known_at < ?", (cutoff_text(as_of),)
    ).fetchall()
    return {row["cik"]: row["sic"] for row in rows}


# ── fundamentals ──


def _fact(row: sqlite3.Row) -> StockFact:
    return _read(
        "stock_facts",
        row,
        lambda: StockFact(
            cik=row["cik"],
            input=row["input"],
            period=row["period"],
            period_end=read_date(row["period_end"], "stock_facts", "period_end"),
            value=row["value"],
            accession=row["accession"],
            known_at=read_stamp(row["known_at"], "stock_facts", "known_at"),
        ),
    )


def save_facts(store: Store, facts: Iterable[StockFact]) -> int:
    """Store each value that differs from the newest held for its company, input and period
    (or is the first); return how many were new. Nothing held is changed."""
    saved = 0
    with store.transaction():
        for fact in facts:
            held = store.execute(
                f"SELECT {_FACT_COLUMNS} FROM stock_facts WHERE cik = ? AND input = ? "
                "AND period = ? ORDER BY known_at DESC LIMIT 1",
                (fact.cik, fact.input, fact.period),
            ).fetchone()
            if held is not None:
                same = _fact(held)
                if (same.value, same.period_end, same.accession) == (
                    fact.value,
                    fact.period_end,
                    fact.accession,
                ):
                    continue
            store.execute(
                f"INSERT INTO stock_facts ({_FACT_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT DO NOTHING",
                (
                    fact.cik,
                    fact.input,
                    fact.period,
                    date_text(fact.period_end),
                    fact.value,
                    fact.accession,
                    stamp_text(fact.known_at),
                ),
            )
            saved += 1
    return saved


def facts(store: Store, *, as_of: date) -> tuple[StockFact, ...]:
    """For each company, input and period, the newest value read by the end of ``as_of``."""
    rows = store.execute(
        f"SELECT {_FACT_COLUMNS} FROM stock_facts f WHERE known_at = ("
        "SELECT MAX(known_at) FROM stock_facts g WHERE g.cik = f.cik AND g.input = f.input "
        "AND g.period = f.period AND g.known_at < ?) ORDER BY cik, input, period",
        (cutoff_text(as_of),),
    ).fetchall()
    return tuple(_fact(row) for row in rows)


# ── bars ──


def last_sessions(store: Store) -> dict[str, date]:
    """Each symbol's newest session held (what its next read starts from)."""
    rows = store.execute("SELECT symbol, MAX(session) AS last FROM stock_bars GROUP BY symbol")
    return {row["symbol"]: read_date(row["last"], "stock_bars", "session") for row in rows}


def save_bars(store: Store, bars: Sequence[StockBar]) -> int:
    """Store the sessions not held yet; a session held is never replaced."""
    if not bars:
        return 0
    with store.transaction():
        before = store.connection.total_changes
        store.executemany(
            f"INSERT INTO stock_bars ({_BAR_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (symbol, session) DO NOTHING",
            [
                (
                    bar.symbol,
                    date_text(bar.session),
                    bar.close,
                    bar.volume,
                    bar.change,
                    stamp_text(bar.known_at),
                )
                for bar in bars
            ],
        )
        return store.connection.total_changes - before


def first_session(store: Store, *, as_of: date) -> date | None:
    """The oldest session of any bar collected by the end of ``as_of``."""
    row = store.execute(
        "SELECT MIN(session) AS first FROM stock_bars WHERE known_at < ? AND session <= ?",
        (cutoff_text(as_of), date_text(as_of)),
    ).fetchone()
    return (
        None
        if row is None or row["first"] is None
        else read_date(row["first"], "stock_bars", "session")
    )


def bars_since(store: Store, *, since: date, as_of: date) -> tuple[HeldBar, ...]:
    """Every bar of a session from ``since`` to ``as_of`` collected by the end of ``as_of``, by
    symbol and session."""
    rows = store.execute(
        "SELECT symbol, session, close, volume, change FROM stock_bars "
        "WHERE known_at < ? AND session >= ? AND session <= ? ORDER BY symbol, session",
        (cutoff_text(as_of), date_text(since), date_text(as_of)),
    ).fetchall()
    return tuple(
        HeldBar(row[0], date.fromisoformat(row[1]), row[2], row[3], row[4]) for row in rows
    )


def recent_bars(store: Store, *, sessions: int, as_of: date) -> tuple[HeldBar, ...]:
    """Each symbol's last ``sessions`` bars to ``as_of`` collected by its end, by symbol and
    session."""
    rows = store.execute(
        "SELECT symbol, session, close, volume, change FROM ("
        "SELECT symbol, session, close, volume, change, ROW_NUMBER() OVER ("
        "PARTITION BY symbol ORDER BY session DESC) AS n FROM stock_bars "
        "WHERE known_at < ? AND session <= ?) WHERE n <= ? ORDER BY symbol, session",
        (cutoff_text(as_of), date_text(as_of), sessions),
    ).fetchall()
    return tuple(
        HeldBar(row[0], date.fromisoformat(row[1]), row[2], row[3], row[4]) for row in rows
    )
