"""The SEC's filings as the events collector keeps them (``024_sec_filings``; DECISIONS D189,
D200): each filing of a kept form, each pricing prospectus as read and each report section read
for a dossier.

Every table is append-only and every read is bounded by ``as_of`` through ``known_at``: what was
collected after the end of that day changes nothing.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import json
import sqlite3
from collections.abc import Callable, Collection, Iterable
from datetime import date
from decimal import Decimal

from ..domain.filings import Filing, FilingText, IpoTerms
from ..errors import StoreError
from .db import Store
from .mappers import (
    cutoff_text,
    date_text,
    optional_date_text,
    optional_stamp_text,
    read_date,
    read_optional_date,
    read_optional_stamp,
    read_stamp,
    stamp_text,
)

_COLUMNS = (
    "accession",
    "cik",
    "company",
    "form",
    "filed",
    "path",
    "items",
    "accepted_at",
    "known_at",
)
_FILING_COLUMNS = ", ".join(_COLUMNS)
_JOINED_COLUMNS = ", ".join(f"f.{name}" for name in _COLUMNS)


def _read[T](table: str, row: sqlite3.Row, make: Callable[[], T]) -> T:
    try:
        return make()
    except (ValueError, ArithmeticError) as error:
        raise StoreError(f"{table} row {tuple(row)[:2]!r} cannot be read: {error}") from None


def _filing(row: sqlite3.Row) -> Filing:
    items = row["items"]
    return _read(
        "sec_filings",
        row,
        lambda: Filing(
            accession=row["accession"],
            cik=row["cik"],
            company=row["company"],
            form=row["form"],
            filed=read_date(row["filed"], "sec_filings", "filed"),
            path=row["path"],
            items=None if items is None else tuple(json.loads(items)),
            accepted_at=read_optional_stamp(row["accepted_at"], "sec_filings", "accepted_at"),
            known_at=read_stamp(row["known_at"], "sec_filings", "known_at"),
        ),
    )


# ── filings ──


def held_accessions(store: Store, accessions: Iterable[str]) -> set[str]:
    """Which of ``accessions`` the store holds already."""
    wanted = list(accessions)
    if not wanted:
        return set()
    marks = ", ".join("?" * len(wanted))
    rows = store.execute(
        f"SELECT accession FROM sec_filings WHERE accession IN ({marks})", wanted
    ).fetchall()
    return {row["accession"] for row in rows}


def save_filings(store: Store, filings: Iterable[Filing]) -> int:
    """Store the filings not held yet; returns how many were new."""
    rows = [
        (
            f.accession,
            f.cik,
            f.company,
            f.form,
            date_text(f.filed),
            f.path,
            None if f.items is None else json.dumps(list(f.items)),
            optional_stamp_text(f.accepted_at),
            stamp_text(f.known_at),
        )
        for f in filings
    ]
    with store.transaction():
        before = store.execute("SELECT count(*) FROM sec_filings").fetchone()[0]
        store.executemany(
            f"INSERT OR IGNORE INTO sec_filings ({_FILING_COLUMNS}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        after = store.execute("SELECT count(*) FROM sec_filings").fetchone()[0]
    return int(after - before)


def filings(
    store: Store, *, as_of: date, since: date, forms: Collection[str] | None = None
) -> list[Filing]:
    """The filings known by the end of ``as_of`` and filed from ``since``, oldest first."""
    rows = store.execute(
        f"SELECT {_FILING_COLUMNS} FROM sec_filings WHERE known_at < ? AND filed >= ? "
        "ORDER BY filed, accession",
        (cutoff_text(as_of), date_text(since)),
    ).fetchall()
    found = [_filing(row) for row in rows]
    return found if forms is None else [f for f in found if f.form in forms]


def unread_prospectuses(
    store: Store, *, forms: Collection[str], ciks: Collection[int], since: date, limit: int
) -> list[Filing]:
    """Pricing prospectuses filed from ``since`` by one of ``ciks`` that were never read, oldest
    first."""
    rows = store.execute(
        f"SELECT {_JOINED_COLUMNS} "
        "FROM sec_filings f LEFT JOIN ipo_terms t ON t.accession = f.accession "
        "WHERE t.accession IS NULL AND f.filed >= ? ORDER BY f.filed, f.accession",
        (date_text(since),),
    ).fetchall()
    found = [_filing(row) for row in rows]
    return [f for f in found if f.form in forms and f.cik in ciks][:limit]


# ── pricing prospectuses ──


def save_ipo_terms(store: Store, terms: IpoTerms) -> None:
    with store.transaction():
        store.execute(
            "INSERT INTO ipo_terms (accession, cik, status, price, shares, lockup_days, "
            "prospectus_day, lockup_ends, detail, known_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                terms.accession,
                terms.cik,
                terms.status,
                None if terms.price is None else str(terms.price),
                terms.shares,
                terms.lockup_days,
                optional_date_text(terms.prospectus_day),
                optional_date_text(terms.lockup_ends),
                terms.detail,
                stamp_text(terms.known_at),
            ),
        )


def _terms(row: sqlite3.Row) -> IpoTerms:
    return _read(
        "ipo_terms",
        row,
        lambda: IpoTerms(
            accession=row["accession"],
            cik=row["cik"],
            status=row["status"],
            price=None if row["price"] is None else Decimal(row["price"]),
            shares=row["shares"],
            lockup_days=row["lockup_days"],
            prospectus_day=read_optional_date(row["prospectus_day"], "ipo_terms", "day"),
            lockup_ends=read_optional_date(row["lockup_ends"], "ipo_terms", "lockup_ends"),
            detail=row["detail"],
            known_at=read_stamp(row["known_at"], "ipo_terms", "known_at"),
        ),
    )


def ipo_terms(store: Store, *, as_of: date) -> dict[str, IpoTerms]:
    """Every initial public offering's terms read by the end of ``as_of``, by accession."""
    rows = store.execute(
        "SELECT accession, cik, status, price, shares, lockup_days, prospectus_day, lockup_ends, "
        "detail, known_at FROM ipo_terms WHERE status = 'ipo' AND known_at < ?",
        (cutoff_text(as_of),),
    ).fetchall()
    return {row["accession"]: _terms(row) for row in rows}


# ── report sections ──


def save_texts(store: Store, texts: Iterable[FilingText]) -> None:
    """Store sections read (a fetch that failed is not stored: it is asked again)."""
    rows = [
        (
            t.accession,
            t.section,
            t.cik,
            t.form,
            date_text(t.filed),
            t.status,
            t.text,
            t.detail,
            stamp_text(t.known_at),
        )
        for t in texts
        if t.status != "failed"
    ]
    with store.transaction():
        store.executemany(
            "INSERT OR IGNORE INTO filing_texts (accession, section, cik, form, filed, status, "
            "text, detail, known_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )


def texts(store: Store, accession: str, *, as_of: date) -> list[FilingText]:
    """The sections of ``accession`` read by the end of ``as_of``."""
    rows = store.execute(
        "SELECT accession, section, cik, form, filed, status, text, detail, known_at "
        "FROM filing_texts WHERE accession = ? AND known_at < ? ORDER BY section",
        (accession, cutoff_text(as_of)),
    ).fetchall()
    return [_text(row) for row in rows]


def company_texts(store: Store, cik: int, *, as_of: date) -> list[FilingText]:
    """Every section of ``cik``'s reports read by the end of ``as_of``, newest filing first."""
    rows = store.execute(
        "SELECT accession, section, cik, form, filed, status, text, detail, known_at "
        "FROM filing_texts WHERE cik = ? AND known_at < ? ORDER BY filed DESC, accession, section",
        (cik, cutoff_text(as_of)),
    ).fetchall()
    return [_text(row) for row in rows]


def _text(row: sqlite3.Row) -> FilingText:
    return _read(
        "filing_texts",
        row,
        lambda: FilingText(
            accession=row["accession"],
            cik=row["cik"],
            form=row["form"],
            filed=read_date(row["filed"], "filing_texts", "filed"),
            section=row["section"],
            status=row["status"],
            text=row["text"],
            detail=row["detail"],
            known_at=read_stamp(row["known_at"], "filing_texts", "known_at"),
        ),
    )
