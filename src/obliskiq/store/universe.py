"""The universe's records: ingestion's refusals, issuers' holdings files, market-scanner rows and
stock candidates' resolutions.

A holdings file is stored whole with its lines, in one transaction, keyed by the date the file
says it is of; a re-download of the same file replaces its lines. Every read of holdings and
scanner rows is bounded twice: by the date a file or scan is *of* and by the moment it was
*downloaded*, so a stock enters a grid cell on the date its file showed it, and never before the
system held that file. A resolution is the system's own state (DECISIONS D46): the latest answer
IBKR's contract search gave for a candidate, read whole.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import sqlite3
from collections.abc import Callable, Sequence
from datetime import date

from ..domain.universe import (
    CandidateResolution,
    Holding,
    HoldingsFile,
    Rejection,
    RejectionOrigin,
    ScannerRow,
)
from ..errors import StoreError
from .db import Store
from .mappers import cutoff_text, date_text, read_date, read_stamp, stamp_text

_FILE_COLUMNS = "fund_symbol, as_of, issuer, url, downloaded_at, content_sha256, rows"
_HOLDING_COLUMNS = (
    "fund_symbol, as_of, line, ticker, name, asset_class, issuer_sector, sector, location, "
    "country, exchange, ibkr_exchange, currency, weight, candidate_key"
)
_SCANNER_COLUMNS = (
    "scanned_on, location, scan_type, rank, market, conid, symbol, listing_exchange, "
    "company_name, candidate_key, scanned_at"
)
_RESOLUTION_COLUMNS = (
    "candidate_key, resolved_at, conid, symbol, listing_exchange, currency, pinned, pinned_for, "
    "failure"
)
_REJECTION_COLUMNS = "rejected_at, symbol, origin, listing_exchange, conid, description, reason"


def _read[T](table: str, row: sqlite3.Row, make: Callable[[], T]) -> T:
    try:
        return make()
    except ValueError as error:
        raise StoreError(f"{table} row {tuple(row)[:3]!r} cannot be read: {error}") from None


# ── rejections ──


def record_rejections(store: Store, rejections: Sequence[Rejection]) -> int:
    """Record instruments refused at ingestion; a repeat of the same refusal is kept once."""
    if not rejections:
        return 0
    with store.transaction():
        store.executemany(
            f"INSERT INTO universe_rejections ({_REJECTION_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (rejected_at, symbol, origin) DO NOTHING",
            [
                (
                    stamp_text(r.rejected_at),
                    r.symbol,
                    r.origin.value,
                    r.listing_exchange,
                    r.conid,
                    r.description,
                    r.reason,
                )
                for r in rejections
            ],
        )
    return len(rejections)


def rejections(store: Store, *, as_of: date) -> tuple[Rejection, ...]:
    """The refusals recorded by the end of ``as_of``, oldest first."""
    rows = store.execute(
        f"SELECT {_REJECTION_COLUMNS} FROM universe_rejections WHERE rejected_at < ? "
        "ORDER BY rejected_at, symbol",
        (cutoff_text(as_of),),
    ).fetchall()
    return tuple(_rejection_from_row(row) for row in rows)


def _rejection_from_row(row: sqlite3.Row) -> Rejection:
    return _read(
        "universe_rejections",
        row,
        lambda: Rejection(
            rejected_at=read_stamp(row["rejected_at"], "universe_rejections", "rejected_at"),
            symbol=row["symbol"],
            origin=RejectionOrigin(row["origin"]),
            listing_exchange=row["listing_exchange"],
            conid=row["conid"],
            description=row["description"],
            reason=row["reason"],
        ),
    )


# ── holdings files ──


def save_holdings(store: Store, file: HoldingsFile, lines: Sequence[Holding]) -> int:
    """Store one holdings file and its lines, replacing a previous download of the same file."""
    if file.rows != len(lines):
        raise StoreError(
            f"{file.fund_symbol} holdings of {file.as_of} claim {file.rows} lines, got {len(lines)}"
        )
    for holding in lines:
        if (holding.fund_symbol, holding.as_of) != (file.fund_symbol, file.as_of):
            raise StoreError(
                f"line {holding.line} belongs to {holding.fund_symbol} of {holding.as_of}, "
                f"not to {file.fund_symbol} of {file.as_of}"
            )
    if len({holding.line for holding in lines}) != len(lines):
        raise StoreError(f"{file.fund_symbol} holdings of {file.as_of} repeat a line number")
    key = (file.fund_symbol, date_text(file.as_of))
    with store.transaction():
        store.execute("DELETE FROM holdings WHERE fund_symbol = ? AND as_of = ?", key)
        store.execute("DELETE FROM holdings_files WHERE fund_symbol = ? AND as_of = ?", key)
        store.execute(
            f"INSERT INTO holdings_files ({_FILE_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                *key,
                file.issuer,
                file.url,
                stamp_text(file.downloaded_at),
                file.content_sha256,
                file.rows,
            ),
        )
        store.executemany(
            f"INSERT INTO holdings ({_HOLDING_COLUMNS}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    *key,
                    h.line,
                    h.ticker,
                    h.name,
                    h.asset_class,
                    h.issuer_sector,
                    h.sector,
                    h.location,
                    h.country,
                    h.exchange,
                    h.ibkr_exchange,
                    h.currency,
                    h.weight,
                    h.candidate_key,
                )
                for h in lines
            ],
        )
    return len(lines)


def _file_from_row(row: sqlite3.Row) -> HoldingsFile:
    return _read(
        "holdings_files",
        row,
        lambda: HoldingsFile(
            fund_symbol=row["fund_symbol"],
            as_of=read_date(row["as_of"], "holdings_files", "as_of"),
            issuer=row["issuer"],
            url=row["url"],
            downloaded_at=read_stamp(row["downloaded_at"], "holdings_files", "downloaded_at"),
            content_sha256=row["content_sha256"],
            rows=row["rows"],
        ),
    )


def _holding_from_row(row: sqlite3.Row) -> Holding:
    return _read(
        "holdings",
        row,
        lambda: Holding(
            fund_symbol=row["fund_symbol"],
            as_of=read_date(row["as_of"], "holdings", "as_of"),
            line=row["line"],
            ticker=row["ticker"],
            name=row["name"],
            asset_class=row["asset_class"],
            issuer_sector=row["issuer_sector"],
            sector=row["sector"],
            location=row["location"],
            country=row["country"],
            exchange=row["exchange"],
            ibkr_exchange=row["ibkr_exchange"],
            currency=row["currency"],
            weight=row["weight"],
            candidate_key=row["candidate_key"],
        ),
    )


#: Each fund's newest file that was both of and downloaded by the bound.
_LATEST_FILES = (
    "SELECT fund_symbol, MAX(as_of) AS as_of FROM holdings_files "
    "WHERE as_of <= ? AND downloaded_at < ? GROUP BY fund_symbol"
)


def holdings_files(store: Store, *, as_of: date) -> tuple[HoldingsFile, ...]:
    """Each fund's newest holdings file the system held on ``as_of``, by fund."""
    rows = store.execute(
        f"SELECT {', '.join(f'f.{c.strip()}' for c in _FILE_COLUMNS.split(','))} "
        f"FROM holdings_files f JOIN ({_LATEST_FILES}) latest "
        "USING (fund_symbol, as_of) ORDER BY f.fund_symbol",
        (date_text(as_of), cutoff_text(as_of)),
    ).fetchall()
    return tuple(_file_from_row(row) for row in rows)


def holdings_of(store: Store, fund_symbol: str, *, as_of: date) -> tuple[Holding, ...]:
    """The lines of ``fund_symbol``'s newest file held on ``as_of``, in the file's order."""
    rows = store.execute(
        f"SELECT {', '.join(f'h.{c.strip()}' for c in _HOLDING_COLUMNS.split(','))} "
        f"FROM holdings h JOIN ({_LATEST_FILES}) latest USING (fund_symbol, as_of) "
        "WHERE h.fund_symbol = ? ORDER BY h.line",
        (date_text(as_of), cutoff_text(as_of), fund_symbol),
    ).fetchall()
    return tuple(_holding_from_row(row) for row in rows)


def candidate_holdings(store: Store, *, as_of: date) -> tuple[Holding, ...]:
    """Every share line of each fund's newest file held on ``as_of``: the candidates' source."""
    rows = store.execute(
        f"SELECT {', '.join(f'h.{c.strip()}' for c in _HOLDING_COLUMNS.split(','))} "
        f"FROM holdings h JOIN ({_LATEST_FILES}) latest USING (fund_symbol, as_of) "
        "WHERE h.candidate_key IS NOT NULL ORDER BY h.fund_symbol, h.line",
        (date_text(as_of), cutoff_text(as_of)),
    ).fetchall()
    return tuple(_holding_from_row(row) for row in rows)


def first_held(store: Store, candidate_key: str, *, as_of: date) -> date | None:
    """The date of the first file held on ``as_of`` that listed ``candidate_key``."""
    row = store.execute(
        "SELECT MIN(h.as_of) FROM holdings h JOIN holdings_files f USING (fund_symbol, as_of) "
        "WHERE h.candidate_key = ? AND f.as_of <= ? AND f.downloaded_at < ?",
        (candidate_key, date_text(as_of), cutoff_text(as_of)),
    ).fetchone()
    return None if row[0] is None else read_date(row[0], "holdings", "as_of")


# ── scanner rows ──


def save_scanner_rows(store: Store, rows: Sequence[ScannerRow]) -> int:
    """Store one or more scanner runs, each replacing an earlier run of its day and scan."""
    if not rows:
        return 0
    runs = {(date_text(r.scanned_on), r.location, r.scan_type) for r in rows}
    with store.transaction():
        for run in sorted(runs):
            store.execute(
                "DELETE FROM scanner_rows WHERE scanned_on = ? AND location = ? AND scan_type = ?",
                run,
            )
        store.executemany(
            f"INSERT INTO scanner_rows ({_SCANNER_COLUMNS}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    date_text(r.scanned_on),
                    r.location,
                    r.scan_type,
                    r.rank,
                    r.market,
                    r.conid,
                    r.symbol,
                    r.listing_exchange,
                    r.company_name,
                    r.candidate_key,
                    stamp_text(r.scanned_at),
                )
                for r in rows
            ],
        )
    return len(rows)


def scanner_rows(store: Store, *, as_of: date) -> tuple[ScannerRow, ...]:
    """The newest run of each scan held on ``as_of``, by location, scan and rank."""
    rows = store.execute(
        f"SELECT {', '.join(f's.{c.strip()}' for c in _SCANNER_COLUMNS.split(','))} "
        "FROM scanner_rows s JOIN ("
        "SELECT location, scan_type, MAX(scanned_on) AS scanned_on FROM scanner_rows "
        "WHERE scanned_on <= ? AND scanned_at < ? GROUP BY location, scan_type"
        ") latest USING (location, scan_type, scanned_on) "
        "ORDER BY s.location, s.scan_type, s.rank",
        (date_text(as_of), cutoff_text(as_of)),
    ).fetchall()
    return tuple(_scanner_from_row(row) for row in rows)


def _scanner_from_row(row: sqlite3.Row) -> ScannerRow:
    return _read(
        "scanner_rows",
        row,
        lambda: ScannerRow(
            scanned_on=read_date(row["scanned_on"], "scanner_rows", "scanned_on"),
            location=row["location"],
            scan_type=row["scan_type"],
            rank=row["rank"],
            market=row["market"],
            conid=row["conid"],
            symbol=row["symbol"],
            listing_exchange=row["listing_exchange"],
            company_name=row["company_name"],
            candidate_key=row["candidate_key"],
            scanned_at=read_stamp(row["scanned_at"], "scanner_rows", "scanned_at"),
        ),
    )


# ── resolutions ──


def record_resolution(store: Store, resolution: CandidateResolution) -> None:
    """Record what the contract search answered for a candidate, replacing an older answer.

    A pin survives a re-resolution that found the contract again: only the owner unpins.
    """
    with store.transaction():
        store.execute(
            f"INSERT INTO candidate_resolutions ({_RESOLUTION_COLUMNS}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (candidate_key) DO UPDATE SET "
            "resolved_at = excluded.resolved_at, conid = excluded.conid, "
            "symbol = excluded.symbol, listing_exchange = excluded.listing_exchange, "
            "currency = excluded.currency, failure = excluded.failure, "
            "pinned = CASE WHEN excluded.conid IS NULL THEN 0 "
            "ELSE MAX(candidate_resolutions.pinned, excluded.pinned) END, "
            "pinned_for = CASE WHEN excluded.conid IS NULL THEN NULL "
            "ELSE COALESCE(excluded.pinned_for, candidate_resolutions.pinned_for) END",
            (
                resolution.candidate_key,
                stamp_text(resolution.resolved_at),
                resolution.conid,
                resolution.symbol,
                resolution.listing_exchange,
                resolution.currency,
                int(resolution.pinned),
                resolution.pinned_for,
                resolution.failure,
            ),
        )


def record_pin(store: Store, candidate_key: str, *, pinned_for: str | None) -> None:
    """Pin a resolved candidate because a thesis needs it, or unpin it (``pinned_for=None``)."""
    with store.transaction():
        current = store.execute(
            "SELECT conid FROM candidate_resolutions WHERE candidate_key = ?", (candidate_key,)
        ).fetchone()
        if current is None or current["conid"] is None:
            raise StoreError(f"candidate {candidate_key!r} has no resolved contract to pin")
        store.execute(
            "UPDATE candidate_resolutions SET pinned = ?, pinned_for = ? WHERE candidate_key = ?",
            (int(pinned_for is not None), pinned_for, candidate_key),
        )


def candidate_resolutions(store: Store) -> dict[str, CandidateResolution]:
    """Every candidate's latest resolution, by candidate key."""
    rows = store.execute(
        f"SELECT {_RESOLUTION_COLUMNS} FROM candidate_resolutions ORDER BY candidate_key"
    ).fetchall()
    return {row["candidate_key"]: _resolution_from_row(row) for row in rows}


def _resolution_from_row(row: sqlite3.Row) -> CandidateResolution:
    return _read(
        "candidate_resolutions",
        row,
        lambda: CandidateResolution(
            candidate_key=row["candidate_key"],
            resolved_at=read_stamp(row["resolved_at"], "candidate_resolutions", "resolved_at"),
            conid=row["conid"],
            symbol=row["symbol"],
            listing_exchange=row["listing_exchange"],
            currency=row["currency"],
            pinned=bool(row["pinned"]),
            pinned_for=row["pinned_for"],
            failure=row["failure"],
        ),
    )
