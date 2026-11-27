"""The analyst's replay in its own store (023_analyst_replay; H15): the SEC's filings index and
today's tickers, the memory probe, each segment's run on a sheet date, and the replay's spend.

Every read of the filings is bounded by its date (``known_at``); the names are read once and
carry no date (a ticker is a key to prices, not a fact on a sheet).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from ..domain.analyst import REPLAY, AnalystCall, CallKind, DraftRecord
from ..domain.sources import ExtractionStatus
from . import analyst as analyst_repo
from .db import Store
from .mappers import cutoff_text, date_text, read_date, read_stamp, stamp_text

type Side = Literal["after", "before"]
type UnitState = Literal["ok", "interrupted"]


@dataclass(frozen=True, slots=True)
class Filing:
    cik: int
    form: str
    filed: date
    name: str
    known_at: datetime


@dataclass(frozen=True, slots=True)
class Name:
    """A company's ticker as the SEC's records gave it when read (``None``: it has none)."""

    cik: int
    ticker: str | None
    exchange: str | None
    read_at: datetime


@dataclass(frozen=True, slots=True)
class ProbeAnswer:
    """One question of the memory probe and what the model answered (``probability`` ``None``
    when the call failed or stayed invalid)."""

    provider: str
    model_id: str
    quarter: str
    seq: int
    sheet_date: date
    ticker: str
    rose: bool
    status: ExtractionStatus
    probability: float | None
    error: str | None
    input_tokens: int
    output_tokens: int
    cost_usd: float
    produced_at: datetime


@dataclass(frozen=True, slots=True)
class Unit:
    """One segment's run on one sheet date."""

    label: str
    segment: str
    sheet_date: date
    side: Side
    state: UnitState
    reason: str
    cost_usd: float
    finished_at: datetime


# ── the filings and the names ──


def save_filings(store: Store, filings: Iterable[Filing]) -> int:
    """Store the filings not held yet; the count newly stored."""
    with store.transaction():
        cursor = store.executemany(
            "INSERT OR IGNORE INTO replay_filings (cik, form, filed, name, known_at) "
            "VALUES (?, ?, ?, ?, ?)",
            [(f.cik, f.form, date_text(f.filed), f.name, stamp_text(f.known_at)) for f in filings],
        )
        return max(0, cursor.rowcount)


def filers(store: Store, *, as_of: date, since: date) -> dict[int, str]:
    """The companies with a filing known by the end of ``as_of`` and filed after ``since``, each
    with the name of its latest such filing."""
    rows = store.execute(
        "SELECT cik, name FROM replay_filings WHERE known_at < ? AND filed > ? ORDER BY cik, filed",
        (cutoff_text(as_of), date_text(since)),
    ).fetchall()
    return {int(row["cik"]): str(row["name"]) for row in rows}


def save_names(store: Store, names: Iterable[Name]) -> int:
    with store.transaction():
        cursor = store.executemany(
            "INSERT OR IGNORE INTO replay_names (cik, ticker, exchange, read_at) "
            "VALUES (?, ?, ?, ?)",
            [(n.cik, n.ticker, n.exchange, stamp_text(n.read_at)) for n in names],
        )
        return max(0, cursor.rowcount)


def _name(row: sqlite3.Row) -> Name:
    return Name(
        cik=int(row["cik"]),
        ticker=row["ticker"],
        exchange=row["exchange"],
        read_at=read_stamp(row["read_at"], "replay_names", "read_at"),
    )


def names(store: Store) -> dict[int, Name]:
    rows = store.execute("SELECT cik, ticker, exchange, read_at FROM replay_names").fetchall()
    return {int(row["cik"]): _name(row) for row in rows}


# ── the probe ──


_PROBE_COLUMNS = (
    "provider, model_id, quarter, seq, sheet_date, ticker, rose, status, probability, error, "
    "input_tokens, output_tokens, cost_usd, produced_at"
)


def record_probe(store: Store, answer: ProbeAnswer) -> None:
    with store.transaction():
        store.execute(
            f"INSERT INTO replay_probes ({_PROBE_COLUMNS}, record) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                answer.provider,
                answer.model_id,
                answer.quarter,
                answer.seq,
                date_text(answer.sheet_date),
                answer.ticker,
                int(answer.rose),
                answer.status.value,
                answer.probability,
                answer.error,
                answer.input_tokens,
                answer.output_tokens,
                answer.cost_usd,
                stamp_text(answer.produced_at),
                REPLAY,
            ),
        )


def probes(store: Store, *, model_id: str | None = None) -> list[ProbeAnswer]:
    rows = store.execute(
        f"SELECT {_PROBE_COLUMNS} FROM replay_probes WHERE (? IS NULL OR model_id = ?) "
        "ORDER BY model_id, quarter, seq",
        (model_id, model_id),
    ).fetchall()
    return [
        ProbeAnswer(
            provider=row["provider"],
            model_id=row["model_id"],
            quarter=row["quarter"],
            seq=int(row["seq"]),
            sheet_date=read_date(row["sheet_date"], "replay_probes", "sheet_date"),
            ticker=row["ticker"],
            rose=bool(row["rose"]),
            status=ExtractionStatus(row["status"]),
            probability=row["probability"],
            error=row["error"],
            input_tokens=int(row["input_tokens"]),
            output_tokens=int(row["output_tokens"]),
            cost_usd=float(row["cost_usd"]),
            produced_at=read_stamp(row["produced_at"], "replay_probes", "produced_at"),
        )
        for row in rows
    ]


# ── the units and the calls ──


def record_unit(store: Store, unit: Unit) -> None:
    with store.transaction():
        store.execute(
            "INSERT INTO replay_units (label, segment, sheet_date, side, state, reason, cost_usd, "
            "finished_at, record) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                unit.label,
                unit.segment,
                date_text(unit.sheet_date),
                unit.side,
                unit.state,
                unit.reason,
                unit.cost_usd,
                stamp_text(unit.finished_at),
                REPLAY,
            ),
        )


def units(store: Store) -> list[Unit]:
    rows = store.execute(
        "SELECT label, segment, sheet_date, side, state, reason, cost_usd, finished_at "
        "FROM replay_units ORDER BY label, sheet_date"
    ).fetchall()
    return [
        Unit(
            label=row["label"],
            segment=row["segment"],
            sheet_date=read_date(row["sheet_date"], "replay_units", "sheet_date"),
            side=row["side"],
            state=row["state"],
            reason=row["reason"],
            cost_usd=float(row["cost_usd"]),
            finished_at=read_stamp(row["finished_at"], "replay_units", "finished_at"),
        )
        for row in rows
    ]


def unit_calls(
    store: Store, *, segment: str, prompt_hash: str, sheet_date: date
) -> list[AnalystCall]:
    """The replay's calls of one segment's run on one sheet date."""
    rows = store.execute(
        "SELECT call_id FROM analyst_calls WHERE record = ? AND segment = ? AND prompt_hash = ? "
        "AND as_of = ? ORDER BY call_id",
        (REPLAY, segment, prompt_hash, date_text(sheet_date)),
    ).fetchall()
    found = (analyst_repo.get_call(store, int(row["call_id"])) for row in rows)
    return [call for call in found if call is not None]


def replay_drafts(store: Store) -> list[tuple[DraftRecord, AnalystCall]]:
    """Every draft of the replay's dossier calls, with its call, in order."""
    rows = store.execute(
        "SELECT call_id FROM analyst_calls WHERE record = ? AND kind = ? ORDER BY call_id",
        (REPLAY, CallKind.DOSSIER.value),
    ).fetchall()
    calls = (analyst_repo.get_call(store, int(row["call_id"])) for row in rows)
    return [
        (draft, call)
        for call in calls
        if call is not None
        for draft in analyst_repo.drafts_of(store, call.call_id)
    ]


def replay_spend(store: Store) -> float:
    """What the replay has spent in all: its analyst calls and its probe's."""
    row = store.execute(
        "SELECT (SELECT COALESCE(SUM(cost_usd), 0) FROM analyst_calls WHERE record = ?) + "
        "(SELECT COALESCE(SUM(cost_usd), 0) FROM replay_probes) AS spent",
        (REPLAY,),
    ).fetchone()
    return float(row["spent"])


# ── the cohorts ──


@dataclass(frozen=True, slots=True)
class StoredCohort:
    """A sheet date's universe over its horizon (``benchmark`` ``None``: not evaluable whole)."""

    sheet_date: date
    start: date
    end: date
    universe: int
    evaluable: int
    reasons: dict[str, int]
    benchmark: float | None


def record_cohorts(store: Store, cohorts: Iterable[StoredCohort], *, at: datetime) -> int:
    """Store the cohorts not held yet; the count newly stored."""
    with store.transaction():
        cursor = store.executemany(
            "INSERT OR IGNORE INTO replay_cohorts (sheet_date, start, end, universe, evaluable, "
            "reasons, benchmark, computed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    date_text(c.sheet_date),
                    date_text(c.start),
                    date_text(c.end),
                    c.universe,
                    c.evaluable,
                    json.dumps(c.reasons, sort_keys=True),
                    c.benchmark,
                    stamp_text(at),
                )
                for c in cohorts
            ],
        )
        return max(0, cursor.rowcount)


def cohorts(store: Store) -> dict[date, StoredCohort]:
    rows = store.execute(
        "SELECT sheet_date, start, end, universe, evaluable, reasons, benchmark "
        "FROM replay_cohorts ORDER BY sheet_date"
    ).fetchall()
    found = {}
    for row in rows:
        day = read_date(row["sheet_date"], "replay_cohorts", "sheet_date")
        found[day] = StoredCohort(
            sheet_date=day,
            start=read_date(row["start"], "replay_cohorts", "start"),
            end=read_date(row["end"], "replay_cohorts", "end"),
            universe=int(row["universe"]),
            evaluable=int(row["evaluable"]),
            reasons={str(k): int(v) for k, v in json.loads(row["reasons"]).items()},
            benchmark=row["benchmark"],
        )
    return found


def average_traded_value(
    store: Store, symbol: str, *, before: date, sessions: int
) -> Decimal | None:
    """A name's average close times volume over its last ``sessions`` sessions before
    ``before``; ``None`` without one."""
    rows = store.execute(
        "SELECT close, volume FROM stock_bars WHERE symbol = ? AND session < ? "
        "AND volume IS NOT NULL ORDER BY session DESC LIMIT ?",
        (symbol, date_text(before), sessions),
    ).fetchall()
    traded = [Decimal(repr(row["close"])) * Decimal(repr(row["volume"])) for row in rows]
    return sum(traded, Decimal(0)) / len(traded) if traded else None


def listing_exchange(store: Store, symbol: str) -> str | None:
    """The SEC's exchange a listing snapshot gave ``symbol``, if one did."""
    row = store.execute(
        "SELECT exchange FROM stock_names WHERE ticker = ? ORDER BY known_at DESC LIMIT 1",
        (symbol,),
    ).fetchone()
    return None if row is None else str(row["exchange"])


def stock_bar_rows(store: Store) -> Iterable[tuple[str, str, float, float | None]]:
    """Every stored bar as (symbol, session, close, change), by symbol then session."""
    for row in store.execute(
        "SELECT symbol, session, close, change FROM stock_bars ORDER BY symbol, session"
    ):
        yield row["symbol"], row["session"], row["close"], row["change"]
