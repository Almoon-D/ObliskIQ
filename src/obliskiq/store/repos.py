"""Repositories: what the store holds, read as of a date and written idempotently.

Every read takes a keyword-only ``as_of`` and returns only what the store held on that date:
bars up to that session, snapshots and chains collected on or before it, macro observations
released on or before it, instruments created (and superseded) by it. The bound is part of
every query, ``COUNT(*)`` included: a count that ignored it would let a gate open on evidence
from its own future. A test enforces both the keyword and that no other package holds SQL.

Writes are upserts keyed by the table's primary key, so a re-run of a collection replaces its
rows instead of duplicating them. They take a :class:`Store` and join its open transaction, if
there is one, so a caller can make several writes atomic.

Ported from the typed reads in ObliskIQ-lite-MCP's ``compute/frames.py``, which left the bound
optional.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Sequence
from datetime import date, datetime

from ..domain.enums import InstrumentRole
from ..domain.instrument import Instrument
from ..domain.macro import MacroObservation, MacroSeries
from ..domain.market import Bar, ChainRow, CurvePoint, Snapshot
from ..errors import StoreError
from .db import Store
from .mappers import (
    BAR_COLUMNS,
    CHAIN_COLUMNS,
    CURVE_COLUMNS,
    INSTRUMENT_COLUMNS,
    MACRO_COLUMNS,
    SNAPSHOT_COLUMNS,
    bar_from_row,
    bar_params,
    chain_params,
    chain_row_from_row,
    curve_params,
    curve_point_from_row,
    cutoff_text,
    date_text,
    instrument_from_row,
    instrument_params,
    macro_from_row,
    macro_params,
    read_date,
    snapshot_from_row,
    snapshot_params,
    stamp_text,
)


def _placeholders(count: int) -> str:
    return ", ".join("?" * count)


def _positive(limit: int | None) -> None:
    if limit is not None and limit <= 0:
        raise StoreError(f"limit must be positive, got {limit}")


def _count(store: Store, sql: str, params: Sequence[object]) -> int:
    row = store.execute(sql, params).fetchone()
    return int(row[0])


# ── instruments ──


def upsert_instrument(store: Store, instrument: Instrument) -> None:
    """Insert an instrument, or update the one with the same conid (its ``created_at`` stays).

    A supersession is never changed here: an update leaves a stored one as it is, and an update
    that names a different replacement is refused (for an existing instrument
    ``supersede_instrument`` is the only writer).
    """
    params = instrument_params(instrument)
    with store.transaction():
        stored = store.execute(
            "SELECT superseded_by FROM instruments WHERE conid = ?", (instrument.conid,)
        ).fetchone()
        if stored is not None and instrument.superseded_by not in (None, stored["superseded_by"]):
            raise StoreError(
                f"instrument {instrument.conid} is superseded through supersede_instrument only"
            )
        store.execute(
            f"INSERT INTO instruments ({INSTRUMENT_COLUMNS}) VALUES ({_placeholders(len(params))}) "
            "ON CONFLICT (conid) DO UPDATE SET symbol = excluded.symbol, "
            "listing_exchange = excluded.listing_exchange, currency = excluded.currency, "
            "asset_class = excluded.asset_class, domicile = excluded.domicile, "
            "ucits = excluded.ucits, kid_status = excluded.kid_status, "
            "share_class = excluded.share_class, lot_size = excluded.lot_size, "
            "option_exchange = excluded.option_exchange, "
            "history_exchange = excluded.history_exchange, roles = excluded.roles",
            params,
        )


def supersede_instrument(store: Store, conid: int, by: int, *, at: datetime) -> None:
    """Record that ``by`` replaces ``conid`` from ``at``. Nothing is deleted.

    Refused when ``conid`` is unknown, already superseded (correct a correction by superseding
    its replacement), or when the replacement would lead back to it.
    """
    with store.transaction():
        row = store.execute(
            "SELECT superseded_by, created_at FROM instruments WHERE conid = ?", (conid,)
        ).fetchone()
        if row is None:
            raise StoreError(f"cannot supersede unknown instrument {conid}")
        if row["superseded_by"] is not None:
            raise StoreError(f"instrument {conid} is already superseded by {row['superseded_by']}")
        _refuse_supersession_cycle(store, conid, by)
        store.execute(
            "UPDATE instruments SET superseded_by = ?, superseded_at = ? WHERE conid = ?",
            (by, stamp_text(at), conid),
        )


def _refuse_supersession_cycle(store: Store, conid: int, replacement: int) -> None:
    """A chain of supersessions that came back to ``conid`` would never end."""
    seen = {conid}
    current: int | None = replacement
    while current is not None:
        if current in seen:
            raise StoreError(f"superseding {conid} by {replacement} would make a cycle")
        seen.add(current)
        row = store.execute(
            "SELECT superseded_by FROM instruments WHERE conid = ?", (current,)
        ).fetchone()
        current = None if row is None else row[0]


def get_instrument(store: Store, conid: int, *, as_of: date) -> Instrument | None:
    """The instrument as the store knew it on ``as_of``; ``None`` if it did not exist yet.

    A supersession recorded after ``as_of`` is not shown: the instrument reads as still standing.
    """
    cutoff = cutoff_text(as_of)
    row = store.execute(
        f"SELECT {INSTRUMENT_COLUMNS} FROM instruments WHERE conid = ? AND created_at < ?",
        (conid, cutoff),
    ).fetchone()
    return None if row is None else instrument_from_row(row, as_of_cutoff=cutoff)


def list_instruments(
    store: Store, *, as_of: date, role: InstrumentRole | None = None, include_superseded: bool
) -> tuple[Instrument, ...]:
    """The instruments that existed on ``as_of``, by conid; superseded ones only on request."""
    cutoff = cutoff_text(as_of)
    sql = f"SELECT {INSTRUMENT_COLUMNS} FROM instruments WHERE created_at < ?"
    params: list[object] = [cutoff]
    if not include_superseded:
        sql += " AND (superseded_at IS NULL OR superseded_at >= ?)"
        params.append(cutoff)
    if role is not None:
        sql += " AND EXISTS (SELECT 1 FROM json_each(instruments.roles) WHERE value = ?)"
        params.append(role.value)
    sql += " ORDER BY conid"
    rows = store.execute(sql, params).fetchall()
    return tuple(instrument_from_row(row, as_of_cutoff=cutoff) for row in rows)


def resolve_instrument(store: Store, conid: int, *, as_of: date) -> Instrument | None:
    """The instrument standing in for ``conid`` on ``as_of``, following supersessions.

    ``None`` when ``conid`` did not exist yet. A chain that loops is a corrupt store and raises.
    """
    seen: set[int] = set()
    current = get_instrument(store, conid, as_of=as_of)
    while current is not None and current.superseded_by is not None:
        if current.conid in seen:
            raise StoreError(f"instrument {conid} is in a supersession cycle")
        seen.add(current.conid)
        following = get_instrument(store, current.superseded_by, as_of=as_of)
        if following is None:
            raise StoreError(
                f"instrument {current.conid} names replacement {current.superseded_by}, "
                "which does not exist"
            )
        current = following
    return current


# ── bars ──


def upsert_bars(store: Store, bars: Sequence[Bar]) -> int:
    """Insert or replace daily bars; returns how many were given."""
    if not bars:
        return 0
    with store.transaction():
        store.executemany(
            f"INSERT INTO bars ({BAR_COLUMNS}) VALUES ({_placeholders(8)}) "
            "ON CONFLICT (conid, session_date) DO UPDATE SET open = excluded.open, "
            "high = excluded.high, low = excluded.low, close = excluded.close, "
            "volume = excluded.volume, source = excluded.source",
            [bar_params(bar) for bar in bars],
        )
    return len(bars)


def bar_series(
    store: Store,
    conid: int,
    *,
    as_of: date,
    since: date | None = None,
    limit: int | None = None,
) -> tuple[Bar, ...]:
    """Daily bars up to and including the session ``as_of``, oldest first.

    ``limit`` keeps the newest bars. Callers pass the last *completed* session as ``as_of``
    (the calendars' ``last_completed_session``), never the day a session is still open.
    """
    _positive(limit)
    sql = f"SELECT {BAR_COLUMNS} FROM bars WHERE conid = ? AND session_date <= ?"
    params: list[object] = [conid, date_text(as_of)]
    if since is not None:
        sql += " AND session_date >= ?"
        params.append(date_text(since))
    if limit is None:
        rows = store.execute(sql + " ORDER BY session_date", params).fetchall()
    else:
        newest = store.execute(sql + " ORDER BY session_date DESC LIMIT ?", [*params, limit])
        rows = list(reversed(newest.fetchall()))
    return tuple(bar_from_row(row) for row in rows)


def latest_bar_date(store: Store, conid: int, *, as_of: date) -> date | None:
    """The newest session with a bar on or before ``as_of``.

    A ``MAX`` and not a windowed read: it answers "how old is the newest bar", and a window
    narrower than the answer would report the stalest instruments as having no bars at all.
    """
    row = store.execute(
        "SELECT MAX(session_date) FROM bars WHERE conid = ? AND session_date <= ?",
        (conid, date_text(as_of)),
    ).fetchone()
    return None if row[0] is None else read_date(row[0], "bars", "session_date")


def bar_count(store: Store, conid: int, *, as_of: date) -> int:
    return _count(
        store,
        "SELECT COUNT(*) FROM bars WHERE conid = ? AND session_date <= ?",
        (conid, date_text(as_of)),
    )


# ── snapshots ──


def upsert_snapshots(store: Store, snapshots: Sequence[Snapshot]) -> int:
    """Insert or replace underlying snapshots; returns how many were given."""
    if not snapshots:
        return 0
    with store.transaction():
        store.executemany(
            f"INSERT INTO snapshots ({SNAPSHOT_COLUMNS}) VALUES ({_placeholders(18)}) "
            "ON CONFLICT (snapshot_date, conid) DO UPDATE SET last = excluded.last, "
            "bid = excluded.bid, ask = excluded.ask, volume = excluded.volume, "
            "implied_vol = excluded.implied_vol, implied_vol_source = excluded.implied_vol_source, "
            "historical_vol_30d = excluded.historical_vol_30d, "
            "dividend_yield = excluded.dividend_yield, "
            "dividend_yield_basis = excluded.dividend_yield_basis, "
            "implied_vol_solved = excluded.implied_vol_solved, "
            "implied_vol_solved_source = excluded.implied_vol_solved_source, "
            "source = excluded.source, collected_at = excluded.collected_at, "
            "market_data_status = excluded.market_data_status, "
            "market_data_raw = excluded.market_data_raw, "
            "quote_delay_seconds = excluded.quote_delay_seconds",
            [snapshot_params(snapshot) for snapshot in snapshots],
        )
    return len(snapshots)


def snapshots(
    store: Store, conid: int, *, as_of: date, limit: int | None = None
) -> tuple[Snapshot, ...]:
    """Snapshots collected on or before ``as_of``, oldest first; ``limit`` keeps the newest."""
    _positive(limit)
    sql = f"SELECT {SNAPSHOT_COLUMNS} FROM snapshots WHERE conid = ? AND snapshot_date <= ?"
    params: list[object] = [conid, date_text(as_of)]
    if limit is None:
        rows = store.execute(sql + " ORDER BY snapshot_date", params).fetchall()
    else:
        newest = store.execute(sql + " ORDER BY snapshot_date DESC LIMIT ?", [*params, limit])
        rows = list(reversed(newest.fetchall()))
    return tuple(snapshot_from_row(row) for row in rows)


def snapshot_count(store: Store, conid: int, *, as_of: date) -> int:
    return _count(
        store,
        "SELECT COUNT(*) FROM snapshots WHERE conid = ? AND snapshot_date <= ?",
        (conid, date_text(as_of)),
    )


def implied_vol_snapshot_count(store: Store, conid: int, *, as_of: date) -> int:
    """Snapshots on or before ``as_of`` that carry a quoted implied volatility.

    This count switches a self-computed percentile on once enough history has accumulated, so
    it takes the same bound as every other read: a count that saw past ``as_of`` would open the
    gate on evidence from its own future.
    """
    return _count(
        store,
        "SELECT COUNT(*) FROM snapshots "
        "WHERE conid = ? AND snapshot_date <= ? AND implied_vol IS NOT NULL",
        (conid, date_text(as_of)),
    )


# ── option chains ──


def upsert_chain_rows(store: Store, rows: Sequence[ChainRow]) -> int:
    """Insert or replace chain rows; returns how many were given."""
    if not rows:
        return 0
    with store.transaction():
        store.executemany(
            f"INSERT INTO chain_snapshots ({CHAIN_COLUMNS}) VALUES ({_placeholders(22)}) "
            'ON CONFLICT (snapshot_date, underlying_conid, expiry, strike, "right") DO UPDATE SET '
            "option_conid = excluded.option_conid, option_symbol = excluded.option_symbol, "
            "option_exchange = excluded.option_exchange, bid = excluded.bid, ask = excluded.ask, "
            "last = excluded.last, iv = excluded.iv, delta = excluded.delta, "
            "open_interest = excluded.open_interest, volume = excluded.volume, "
            "sigma_used = excluded.sigma_used, sigma_source = excluded.sigma_source, "
            "source = excluded.source, collected_at = excluded.collected_at, "
            "market_data_status = excluded.market_data_status, "
            "market_data_raw = excluded.market_data_raw, "
            "quote_delay_seconds = excluded.quote_delay_seconds",
            [chain_params(row) for row in rows],
        )
    return len(rows)


def latest_chain_date(
    store: Store, *, as_of: date, underlying_conid: int | None = None
) -> date | None:
    """The newest chain snapshot on or before ``as_of``."""
    sql = "SELECT MAX(snapshot_date) FROM chain_snapshots WHERE snapshot_date <= ?"
    params: list[object] = [date_text(as_of)]
    if underlying_conid is not None:
        sql += " AND underlying_conid = ?"
        params.append(underlying_conid)
    row = store.execute(sql, params).fetchone()
    return None if row[0] is None else read_date(row[0], "chain_snapshots", "snapshot_date")


def chain_rows(
    store: Store,
    *,
    as_of: date,
    underlying_conid: int | None = None,
    snapshot_date: date | None = None,
    expiry: date | None = None,
) -> tuple[ChainRow, ...]:
    """One session's chain rows: ``snapshot_date`` if given, otherwise the newest by ``as_of``.

    Asking for a ``snapshot_date`` after ``as_of`` is asking for the future and raises.
    """
    if snapshot_date is not None and snapshot_date > as_of:
        raise StoreError(f"snapshot_date {snapshot_date} is after as_of {as_of}")
    session = snapshot_date or latest_chain_date(
        store, as_of=as_of, underlying_conid=underlying_conid
    )
    if session is None:
        return ()
    sql = f"SELECT {CHAIN_COLUMNS} FROM chain_snapshots WHERE snapshot_date = ?"
    params: list[object] = [date_text(session)]
    if underlying_conid is not None:
        sql += " AND underlying_conid = ?"
        params.append(underlying_conid)
    if expiry is not None:
        sql += " AND expiry = ?"
        params.append(date_text(expiry))
    sql += ' ORDER BY underlying_conid, expiry, "right", strike'
    return tuple(chain_row_from_row(row) for row in store.execute(sql, params).fetchall())


def chain_row_count(store: Store, *, as_of: date, underlying_conid: int | None = None) -> int:
    sql = "SELECT COUNT(*) FROM chain_snapshots WHERE snapshot_date <= ?"
    params: list[object] = [date_text(as_of)]
    if underlying_conid is not None:
        sql += " AND underlying_conid = ?"
        params.append(underlying_conid)
    return _count(store, sql, params)


# ── futures curves ──


def upsert_curve_points(store: Store, points: Sequence[CurvePoint]) -> int:
    """Insert or replace curve points; returns how many were given."""
    if not points:
        return 0
    with store.transaction():
        store.executemany(
            f"INSERT INTO curves ({CURVE_COLUMNS}) VALUES ({_placeholders(13)}) "
            "ON CONFLICT (snapshot_date, root, expiry) DO UPDATE SET price = excluded.price, "
            "annualised_roll = excluded.annualised_roll, conid = excluded.conid, "
            "exchange = excluded.exchange, currency = excluded.currency, "
            "source = excluded.source, collected_at = excluded.collected_at, "
            "market_data_status = excluded.market_data_status, "
            "market_data_raw = excluded.market_data_raw, "
            "quote_delay_seconds = excluded.quote_delay_seconds",
            [curve_params(point) for point in points],
        )
    return len(points)


def latest_curve_date(store: Store, root: str, *, as_of: date) -> date | None:
    """The newest curve snapshot for a root on or before ``as_of``."""
    row = store.execute(
        "SELECT MAX(snapshot_date) FROM curves WHERE root = ? AND snapshot_date <= ?",
        (root, date_text(as_of)),
    ).fetchone()
    return None if row[0] is None else read_date(row[0], "curves", "snapshot_date")


def curve(
    store: Store, root: str, *, as_of: date, snapshot_date: date | None = None
) -> tuple[CurvePoint, ...]:
    """One session's term structure for a root, front expiry first.

    Asking for a ``snapshot_date`` after ``as_of`` raises: a curve read for a past date must
    not return today's shape.
    """
    if snapshot_date is not None and snapshot_date > as_of:
        raise StoreError(f"snapshot_date {snapshot_date} is after as_of {as_of}")
    session = snapshot_date or latest_curve_date(store, root, as_of=as_of)
    if session is None:
        return ()
    rows = store.execute(
        f"SELECT {CURVE_COLUMNS} FROM curves WHERE root = ? AND snapshot_date = ? ORDER BY expiry",
        (root, date_text(session)),
    ).fetchall()
    return tuple(curve_point_from_row(row) for row in rows)


def curve_front_history(
    store: Store, root: str, *, as_of: date, since: date | None = None
) -> tuple[tuple[date, float], ...]:
    """The front contract's price per session, oldest first, up to ``as_of``.

    The front is the earliest expiry listed that session; a session whose front has no price
    is left out and does not fall back to the next contract, which would splice two different
    contracts into one series.
    """
    sql = (
        "SELECT c.snapshot_date, c.price FROM curves AS c "
        "WHERE c.root = ? AND c.snapshot_date <= ? AND c.price IS NOT NULL "
        "AND c.expiry = (SELECT MIN(f.expiry) FROM curves AS f "
        "WHERE f.root = c.root AND f.snapshot_date = c.snapshot_date)"
    )
    params: list[object] = [root, date_text(as_of)]
    if since is not None:
        sql += " AND c.snapshot_date >= ?"
        params.append(date_text(since))
    sql += " ORDER BY c.snapshot_date"
    return tuple(
        (read_date(row[0], "curves", "snapshot_date"), float(row[1]))
        for row in store.execute(sql, params).fetchall()
    )


# ── macro series ──


def upsert_macro(store: Store, observations: Sequence[MacroObservation]) -> int:
    """Insert or replace macro observations (one value per period); returns how many were given."""
    if not observations:
        return 0
    with store.transaction():
        store.executemany(
            f"INSERT INTO macro ({MACRO_COLUMNS}) VALUES ({_placeholders(8)}) "
            "ON CONFLICT (series_id, source, reference_date) DO UPDATE SET "
            "release_date = excluded.release_date, value = excluded.value, "
            "frequency = excluded.frequency, units = excluded.units, "
            "collected_at = excluded.collected_at",
            [macro_params(observation) for observation in observations],
        )
    return len(observations)


def macro_series(
    store: Store,
    series_id: str,
    *,
    as_of: date,
    source: str | None = None,
    since: date | None = None,
) -> MacroSeries:
    """The observations of a series that were public on ``as_of``, oldest period first.

    Alignment is on ``release_date``: an observation with no release date cannot be placed in
    time and is left out, counted in ``unaligned`` (those the store already held by ``as_of``)
    so the caller reports the gap instead of aligning it optimistically. A series that several
    sources supply must be asked for by source; mixing them would interleave two series.
    """
    where = "series_id = ?"
    params: list[object] = [series_id]
    if source is not None:
        where += " AND source = ?"
        params.append(source)
    if since is not None:
        where += " AND reference_date >= ?"
        params.append(date_text(since))
    with store.read():
        rows = store.execute(
            f"SELECT {MACRO_COLUMNS} FROM macro WHERE {where} "
            "AND release_date IS NOT NULL AND release_date <= ? ORDER BY reference_date",
            [*params, date_text(as_of)],
        ).fetchall()
        unaligned = _count(
            store,
            f"SELECT COUNT(*) FROM macro WHERE {where} "
            "AND release_date IS NULL AND collected_at < ?",
            [*params, cutoff_text(as_of)],
        )
    observations = tuple(macro_from_row(row) for row in rows)
    sources = {observation.source for observation in observations}
    if source is None and len(sources) > 1:
        raise StoreError(
            f"series {series_id} comes from several sources ({', '.join(sorted(sources))}); "
            "name one"
        )
    return MacroSeries(
        series_id=series_id,
        source=source if source is not None else next(iter(sources), None),
        as_of=as_of,
        observations=observations,
        unaligned=unaligned,
    )
