"""The sources' records: CFTC reports, Form 4 filings, prediction-market snapshots, GDELT days,
news items and their extractions, earnings dates, the halt feed's last state and source health.

Every read of what a source delivered takes a keyword-only ``as_of`` and returns what was public
by its end: a CFTC report by its release date, a filing by its filing date, a snapshot, a GDELT
day, a news item or an earnings read by when it was collected. The health and queue reads are
the system's own state and read it whole.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import json
import sqlite3
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, time

from ..domain.sources import (
    CotReport,
    EarningsDate,
    EventExtraction,
    EventMarket,
    EventSnapshot,
    ExtractionStatus,
    GdeltDay,
    InsiderFiling,
    InsiderOwner,
    InsiderTransaction,
    NewsItem,
    RunVerdict,
    SourceRun,
)
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

_COT_COLUMNS = (
    "contract_code, report_date, release_date, market_name, open_interest, noncomm_long, "
    "noncomm_short, comm_long, comm_short, nonrept_long, nonrept_short, source, collected_at"
)
_FILING_COLUMNS = (
    "accession, form, issuer_cik, issuer_symbol, filed_on, accepted_at, period_of_report, "
    "collected_at"
)
_OWNER_COLUMNS = (
    "accession, owner_cik, owner_name, is_director, is_officer, is_ten_percent, is_other, "
    "officer_title"
)
_TRANSACTION_COLUMNS = (
    "accession, line, derivative, security_title, transaction_date, code, shares, price, "
    "acquired, shares_after, direct"
)
_MARKET_COLUMNS = (
    "venue, market_id, event_id, question, outcome, closes_at, curated, tags, first_seen_at, "
    "last_seen_at"
)
_SNAPSHOT_COLUMNS = (
    "venue, market_id, observed_at, probability, bid, ask, last_price, volume, liquidity"
)
_GDELT_COLUMNS = "query_id, day, articles, monitored, tone, collected_at"
_NEWS_COLUMNS = (
    "item_id, feed, kind, region, symbol, guid, link, title, summary, published_at, "
    "collected_at, raw, fallback_tone"
)
_EXTRACTION_COLUMNS = (
    "item_id, produced_at, provider, model_id, prompt_version, prompt_hash, status, attempts, "
    "output, error, input_tokens, output_tokens, cost_usd"
)
_HEALTH_COLUMNS = (
    "source, last_attempt_at, last_success_at, last_status, last_rows, consecutive_failures, detail"
)
#: Run verdicts that count as a success for a source's health.
_SUCCEEDED = frozenset({"ok", "degraded", "partial"})


def _read[T](table: str, row: sqlite3.Row, make: Callable[[], T]) -> T:
    try:
        return make()
    except ValueError as error:
        raise StoreError(f"{table} row {tuple(row)[:3]!r} cannot be read: {error}") from None


def _flag(value: bool | None) -> int | None:
    return None if value is None else int(value)


def _bool(value: int | None) -> bool | None:
    return None if value is None else bool(value)


# ── source health ──


def record_source_run(
    store: Store, source: str, *, at: datetime, status: RunVerdict, rows: int, detail: str | None
) -> SourceRun:
    """Record a source's run; a success resets its failures, a failure counts one more."""
    succeeded = status in _SUCCEEDED
    with store.transaction():
        held = store.execute(
            "SELECT last_success_at, consecutive_failures FROM source_health WHERE source = ?",
            (source,),
        ).fetchone()
        last_success = (
            stamp_text(at) if succeeded else (None if held is None else held["last_success_at"])
        )
        failures = 0 if succeeded else (1 if held is None else held["consecutive_failures"] + 1)
        store.execute(
            f"INSERT INTO source_health ({_HEALTH_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (source) DO UPDATE SET last_attempt_at = excluded.last_attempt_at, "
            "last_success_at = excluded.last_success_at, last_status = excluded.last_status, "
            "last_rows = excluded.last_rows, "
            "consecutive_failures = excluded.consecutive_failures, detail = excluded.detail",
            (source, stamp_text(at), last_success, status, rows, failures, detail),
        )
    return SourceRun(
        source=source,
        last_attempt_at=at,
        last_success_at=read_optional_stamp(last_success, "source_health", "last_success_at"),
        last_status=status,
        last_rows=rows,
        consecutive_failures=failures,
        detail=detail,
    )


def source_runs(store: Store) -> dict[str, SourceRun]:
    """Every source's last recorded run, by source."""
    rows = store.execute(f"SELECT {_HEALTH_COLUMNS} FROM source_health ORDER BY source").fetchall()
    return {row["source"]: _run_from_row(row) for row in rows}


def _run_from_row(row: sqlite3.Row) -> SourceRun:
    return _read(
        "source_health",
        row,
        lambda: SourceRun(
            source=row["source"],
            last_attempt_at=read_stamp(row["last_attempt_at"], "source_health", "last_attempt_at"),
            last_success_at=read_optional_stamp(
                row["last_success_at"], "source_health", "last_success_at"
            ),
            last_status=row["last_status"],
            last_rows=row["last_rows"],
            consecutive_failures=row["consecutive_failures"],
            detail=row["detail"],
        ),
    )


def record_target(
    store: Store,
    source: str,
    target: str,
    *,
    at: datetime,
    ok: bool,
    rows: int,
    detail: str | None,
) -> None:
    """Record one target's attempt; its last success survives a failure."""
    with store.transaction():
        store.execute(
            "INSERT INTO source_targets (source, target, last_attempt_at, last_success_at, "
            "last_ok, last_rows, detail) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (source, target) DO UPDATE SET "
            "last_attempt_at = excluded.last_attempt_at, "
            "last_success_at = COALESCE(excluded.last_success_at, source_targets.last_success_at), "
            "last_ok = excluded.last_ok, last_rows = excluded.last_rows, detail = excluded.detail",
            (source, target, stamp_text(at), stamp_text(at) if ok else None, int(ok), rows, detail),
        )


def target_succeeded_at(store: Store, source: str, target: str) -> datetime | None:
    """When a target last succeeded, if its last attempt did; ``None`` otherwise."""
    row = store.execute(
        "SELECT last_success_at, last_ok FROM source_targets WHERE source = ? AND target = ?",
        (source, target),
    ).fetchone()
    if row is None or not row["last_ok"]:
        return None
    return read_optional_stamp(row["last_success_at"], "source_targets", "last_success_at")


def succeeded_targets(store: Store, source: str, prefix: str) -> tuple[str, ...]:
    """The targets of a source, named with ``prefix``, whose last attempt succeeded."""
    rows = store.execute(
        "SELECT target FROM source_targets WHERE source = ? AND last_ok = 1 "
        "AND substr(target, 1, ?) = ? ORDER BY target",
        (source, len(prefix), prefix),
    ).fetchall()
    return tuple(row["target"] for row in rows)


# ── CFTC positioning ──


def upsert_cot_reports(store: Store, reports: Sequence[CotReport]) -> int:
    """Insert or replace weekly reports (a revised report replaces the one held)."""
    if not reports:
        return 0
    with store.transaction():
        store.executemany(
            f"INSERT INTO cot_reports ({_COT_COLUMNS}) VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (contract_code, report_date) DO UPDATE SET "
            "release_date = excluded.release_date, market_name = excluded.market_name, "
            "open_interest = excluded.open_interest, noncomm_long = excluded.noncomm_long, "
            "noncomm_short = excluded.noncomm_short, comm_long = excluded.comm_long, "
            "comm_short = excluded.comm_short, nonrept_long = excluded.nonrept_long, "
            "nonrept_short = excluded.nonrept_short, source = excluded.source, "
            "collected_at = excluded.collected_at",
            [
                (
                    r.contract_code,
                    date_text(r.report_date),
                    date_text(r.release_date),
                    r.market_name,
                    r.open_interest,
                    r.noncomm_long,
                    r.noncomm_short,
                    r.comm_long,
                    r.comm_short,
                    r.nonrept_long,
                    r.nonrept_short,
                    r.source,
                    stamp_text(r.collected_at),
                )
                for r in reports
            ],
        )
    return len(reports)


def cot_reports(
    store: Store, contract_code: str, *, as_of: date, since: date | None = None
) -> tuple[CotReport, ...]:
    """A contract's reports released by ``as_of`` (from ``since``'s report on), oldest first."""
    sql = f"SELECT {_COT_COLUMNS} FROM cot_reports WHERE contract_code = ? AND release_date <= ?"
    params: list[object] = [contract_code, date_text(as_of)]
    if since is not None:
        sql += " AND report_date >= ?"
        params.append(date_text(since))
    rows = store.execute(sql + " ORDER BY report_date", params).fetchall()
    return tuple(_cot_from_row(row) for row in rows)


def newest_cot_report(store: Store, contract_code: str) -> date | None:
    """The newest report date held for a contract (the incremental read starts from it)."""
    row = store.execute(
        "SELECT MAX(report_date) AS newest FROM cot_reports WHERE contract_code = ?",
        (contract_code,),
    ).fetchone()
    return read_optional_date(row["newest"], "cot_reports", "report_date")


def _cot_from_row(row: sqlite3.Row) -> CotReport:
    return _read(
        "cot_reports",
        row,
        lambda: CotReport(
            contract_code=row["contract_code"],
            report_date=read_date(row["report_date"], "cot_reports", "report_date"),
            release_date=read_date(row["release_date"], "cot_reports", "release_date"),
            market_name=row["market_name"],
            open_interest=row["open_interest"],
            noncomm_long=row["noncomm_long"],
            noncomm_short=row["noncomm_short"],
            comm_long=row["comm_long"],
            comm_short=row["comm_short"],
            nonrept_long=row["nonrept_long"],
            nonrept_short=row["nonrept_short"],
            source=row["source"],
            collected_at=read_stamp(row["collected_at"], "cot_reports", "collected_at"),
        ),
    )


# ── insiders ──


def has_insider_filing(store: Store, accession: str) -> bool:
    row = store.execute(
        "SELECT 1 FROM insider_filings WHERE accession = ?", (accession,)
    ).fetchone()
    return row is not None


def save_insider_filing(store: Store, filing: InsiderFiling) -> bool:
    """Store a filing with its owners and transactions; one already held is left as it is.
    Returns whether it was new."""
    with store.transaction():
        if has_insider_filing(store, filing.accession):
            return False
        store.execute(
            f"INSERT INTO insider_filings ({_FILING_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                filing.accession,
                filing.form,
                filing.issuer_cik,
                filing.issuer_symbol,
                date_text(filing.filed_on),
                stamp_text(filing.accepted_at),
                optional_date_text(filing.period_of_report),
                stamp_text(filing.collected_at),
            ),
        )
        store.executemany(
            f"INSERT INTO insider_owners ({_OWNER_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    filing.accession,
                    o.owner_cik,
                    o.owner_name,
                    int(o.is_director),
                    int(o.is_officer),
                    int(o.is_ten_percent),
                    int(o.is_other),
                    o.officer_title,
                )
                for o in filing.owners
            ],
        )
        store.executemany(
            f"INSERT INTO insider_transactions ({_TRANSACTION_COLUMNS}) VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    filing.accession,
                    t.line,
                    int(t.derivative),
                    t.security_title,
                    date_text(t.transaction_date),
                    t.code,
                    t.shares,
                    t.price,
                    _flag(t.acquired),
                    t.shares_after,
                    _flag(t.direct),
                )
                for t in filing.transactions
            ],
        )
    return True


def insider_filings(
    store: Store, issuer_cik: int, *, as_of: date, since: date | None = None
) -> tuple[InsiderFiling, ...]:
    """An issuer's filings public by ``as_of`` (filed on or after ``since``), by filing date."""
    sql = f"SELECT {_FILING_COLUMNS} FROM insider_filings WHERE issuer_cik = ? AND filed_on <= ?"
    params: list[object] = [issuer_cik, date_text(as_of)]
    if since is not None:
        sql += " AND filed_on >= ?"
        params.append(date_text(since))
    with store.read():
        rows = store.execute(sql + " ORDER BY filed_on, accession", params).fetchall()
        return tuple(_filing_from_row(store, row) for row in rows)


def _filing_from_row(store: Store, row: sqlite3.Row) -> InsiderFiling:
    accession = row["accession"]
    owners = store.execute(
        f"SELECT {_OWNER_COLUMNS} FROM insider_owners WHERE accession = ? ORDER BY owner_cik",
        (accession,),
    ).fetchall()
    lines = store.execute(
        f"SELECT {_TRANSACTION_COLUMNS} FROM insider_transactions WHERE accession = ? "
        "ORDER BY line",
        (accession,),
    ).fetchall()
    return _read(
        "insider_filings",
        row,
        lambda: InsiderFiling(
            accession=accession,
            form=row["form"],
            issuer_cik=row["issuer_cik"],
            issuer_symbol=row["issuer_symbol"],
            filed_on=read_date(row["filed_on"], "insider_filings", "filed_on"),
            accepted_at=read_stamp(row["accepted_at"], "insider_filings", "accepted_at"),
            period_of_report=read_optional_date(
                row["period_of_report"], "insider_filings", "period_of_report"
            ),
            owners=tuple(
                InsiderOwner(
                    owner_cik=o["owner_cik"],
                    owner_name=o["owner_name"],
                    is_director=bool(o["is_director"]),
                    is_officer=bool(o["is_officer"]),
                    is_ten_percent=bool(o["is_ten_percent"]),
                    is_other=bool(o["is_other"]),
                    officer_title=o["officer_title"],
                )
                for o in owners
            ),
            transactions=tuple(
                InsiderTransaction(
                    line=t["line"],
                    derivative=bool(t["derivative"]),
                    security_title=t["security_title"],
                    transaction_date=read_date(
                        t["transaction_date"], "insider_transactions", "transaction_date"
                    ),
                    code=t["code"],
                    shares=t["shares"],
                    price=t["price"],
                    acquired=_bool(t["acquired"]),
                    shares_after=t["shares_after"],
                    direct=_bool(t["direct"]),
                )
                for t in lines
            ),
            collected_at=read_stamp(row["collected_at"], "insider_filings", "collected_at"),
        ),
    )


# ── prediction markets ──


def save_event_markets(store: Store, markets: Sequence[EventMarket], *, seen_at: datetime) -> int:
    """Record the markets seen; one seen before keeps its first sighting and takes the rest."""
    if not markets:
        return 0
    seen = stamp_text(seen_at)
    with store.transaction():
        store.executemany(
            f"INSERT INTO event_markets ({_MARKET_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (venue, market_id) DO UPDATE SET event_id = excluded.event_id, "
            "question = excluded.question, outcome = excluded.outcome, "
            "closes_at = excluded.closes_at, "
            "curated = MAX(event_markets.curated, excluded.curated), tags = excluded.tags, "
            "last_seen_at = excluded.last_seen_at",
            [
                (
                    m.venue,
                    m.market_id,
                    m.event_id,
                    m.question,
                    m.outcome,
                    optional_stamp_text(m.closes_at),
                    int(m.curated),
                    json.dumps(list(m.tags)),
                    seen,
                    seen,
                )
                for m in markets
            ],
        )
    return len(markets)


def save_event_snapshots(store: Store, snapshots: Sequence[EventSnapshot]) -> int:
    """Record snapshots; one already held for the same instant is kept as it was."""
    if not snapshots:
        return 0
    with store.transaction():
        store.executemany(
            f"INSERT INTO event_snapshots ({_SNAPSHOT_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (venue, market_id, observed_at) DO NOTHING",
            [
                (
                    s.venue,
                    s.market_id,
                    stamp_text(s.observed_at),
                    s.probability,
                    s.bid,
                    s.ask,
                    s.last_price,
                    s.volume,
                    s.liquidity,
                )
                for s in snapshots
            ],
        )
    return len(snapshots)


def event_markets(store: Store, *, as_of: date) -> tuple[EventMarket, ...]:
    """The markets first seen by the end of ``as_of``."""
    rows = store.execute(
        f"SELECT {_MARKET_COLUMNS} FROM event_markets WHERE first_seen_at < ? "
        "ORDER BY venue, market_id",
        (cutoff_text(as_of),),
    ).fetchall()
    return tuple(_market_from_row(row) for row in rows)


def _market_from_row(row: sqlite3.Row) -> EventMarket:
    return _read(
        "event_markets",
        row,
        lambda: EventMarket(
            venue=row["venue"],
            market_id=row["market_id"],
            event_id=row["event_id"],
            question=row["question"],
            outcome=row["outcome"],
            closes_at=read_optional_stamp(row["closes_at"], "event_markets", "closes_at"),
            curated=bool(row["curated"]),
            tags=tuple(json.loads(row["tags"])),
        ),
    )


def event_snapshots(
    store: Store, venue: str, market_id: str, *, as_of: date
) -> tuple[EventSnapshot, ...]:
    """A market's snapshots observed by the end of ``as_of``, oldest first."""
    rows = store.execute(
        f"SELECT {_SNAPSHOT_COLUMNS} FROM event_snapshots WHERE venue = ? AND market_id = ? "
        "AND observed_at < ? ORDER BY observed_at",
        (venue, market_id, cutoff_text(as_of)),
    ).fetchall()
    return tuple(_snapshot_from_row(row) for row in rows)


def _snapshot_from_row(row: sqlite3.Row) -> EventSnapshot:
    return _read(
        "event_snapshots",
        row,
        lambda: EventSnapshot(
            venue=row["venue"],
            market_id=row["market_id"],
            observed_at=read_stamp(row["observed_at"], "event_snapshots", "observed_at"),
            probability=row["probability"],
            bid=row["bid"],
            ask=row["ask"],
            last_price=row["last_price"],
            volume=row["volume"],
            liquidity=row["liquidity"],
        ),
    )


# ── GDELT ──


def save_gdelt_days(store: Store, days: Sequence[GdeltDay]) -> int:
    """Store complete days; a day already held keeps its first reading. Returns how many new."""
    if not days:
        return 0
    with store.transaction():
        before = store.connection.total_changes
        store.executemany(
            f"INSERT INTO gdelt_days ({_GDELT_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (query_id, day) DO NOTHING",
            [
                (
                    d.query_id,
                    date_text(d.day),
                    d.articles,
                    d.monitored,
                    d.tone,
                    stamp_text(d.collected_at),
                )
                for d in days
            ],
        )
        return store.connection.total_changes - before


def gdelt_days(store: Store, query_id: str, *, as_of: date) -> tuple[GdeltDay, ...]:
    """A query's days collected by the end of ``as_of``, oldest day first."""
    rows = store.execute(
        f"SELECT {_GDELT_COLUMNS} FROM gdelt_days WHERE query_id = ? AND collected_at < ? "
        "ORDER BY day",
        (query_id, cutoff_text(as_of)),
    ).fetchall()
    return tuple(_gdelt_from_row(row) for row in rows)


def _gdelt_from_row(row: sqlite3.Row) -> GdeltDay:
    return _read(
        "gdelt_days",
        row,
        lambda: GdeltDay(
            query_id=row["query_id"],
            day=read_date(row["day"], "gdelt_days", "day"),
            articles=row["articles"],
            monitored=row["monitored"],
            tone=row["tone"],
            collected_at=read_stamp(row["collected_at"], "gdelt_days", "collected_at"),
        ),
    )


# ── news ──


def save_news_items(store: Store, items: Sequence[NewsItem]) -> int:
    """Store items not seen before (by id); returns how many were new."""
    if not items:
        return 0
    with store.transaction():
        before = store.connection.total_changes
        store.executemany(
            f"INSERT INTO news_items ({_NEWS_COLUMNS}) VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (item_id) DO NOTHING",
            [
                (
                    i.item_id,
                    i.feed,
                    i.kind,
                    i.region,
                    i.symbol,
                    i.guid,
                    i.link,
                    i.title,
                    i.summary,
                    optional_stamp_text(i.published_at),
                    stamp_text(i.collected_at),
                    i.raw,
                    i.fallback_tone,
                )
                for i in items
            ],
        )
        return store.connection.total_changes - before


def held_news_items(store: Store, item_ids: Sequence[str]) -> frozenset[str]:
    """Which of ``item_ids`` are held already."""
    held: set[str] = set()
    for item_id in item_ids:
        row = store.execute("SELECT 1 FROM news_items WHERE item_id = ?", (item_id,)).fetchone()
        if row is not None:
            held.add(item_id)
    return frozenset(held)


def news_items(store: Store, *, as_of: date, since: date | None = None) -> tuple[NewsItem, ...]:
    """The items collected by the end of ``as_of`` (from ``since`` on), oldest first."""
    sql = f"SELECT {_NEWS_COLUMNS} FROM news_items WHERE collected_at < ?"
    params: list[object] = [cutoff_text(as_of)]
    if since is not None:
        sql += " AND collected_at >= ?"
        params.append(stamp_text(datetime.combine(since, time.min, tzinfo=UTC)))
    rows = store.execute(sql + " ORDER BY collected_at, item_id", params).fetchall()
    return tuple(_news_from_row(row) for row in rows)


def _news_from_row(row: sqlite3.Row) -> NewsItem:
    return _read(
        "news_items",
        row,
        lambda: NewsItem(
            item_id=row["item_id"],
            feed=row["feed"],
            kind=row["kind"],
            region=row["region"],
            symbol=row["symbol"],
            guid=row["guid"],
            link=row["link"],
            title=row["title"],
            summary=row["summary"],
            published_at=read_optional_stamp(row["published_at"], "news_items", "published_at"),
            collected_at=read_stamp(row["collected_at"], "news_items", "collected_at"),
            raw=row["raw"],
            fallback_tone=row["fallback_tone"],
        ),
    )


def pending_news_items(
    store: Store, *, model_id: str, prompt_hash: str, limit: int, max_failures: int
) -> tuple[NewsItem, ...]:
    """The items the extractor still owes for one segment (model and prompt), oldest first:
    no ``ok`` or ``invalid`` extraction yet, and fewer than ``max_failures`` failed calls."""
    rows = store.execute(
        f"SELECT {', '.join('n.' + c.strip() for c in _NEWS_COLUMNS.split(','))} "
        "FROM news_items n WHERE NOT EXISTS (SELECT 1 FROM event_extractions e "
        "WHERE e.item_id = n.item_id AND e.model_id = ? AND e.prompt_hash = ? "
        "AND e.status IN ('ok', 'invalid')) "
        "AND (SELECT COUNT(*) FROM event_extractions f WHERE f.item_id = n.item_id "
        "AND f.model_id = ? AND f.prompt_hash = ? AND f.status = 'failed') < ? "
        "ORDER BY n.collected_at, n.item_id LIMIT ?",
        (model_id, prompt_hash, model_id, prompt_hash, max_failures, limit),
    ).fetchall()
    return tuple(_news_from_row(row) for row in rows)


def record_extraction(store: Store, extraction: EventExtraction) -> None:
    """Append one extractor call (the table refuses an update or a delete)."""
    with store.transaction():
        store.execute(
            f"INSERT INTO event_extractions ({_EXTRACTION_COLUMNS}) VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                extraction.item_id,
                stamp_text(extraction.produced_at),
                extraction.provider,
                extraction.model_id,
                extraction.prompt_version,
                extraction.prompt_hash,
                extraction.status.value,
                extraction.attempts,
                extraction.output,
                extraction.error,
                extraction.input_tokens,
                extraction.output_tokens,
                extraction.cost_usd,
            ),
        )


def extraction_spend(store: Store, since: datetime) -> float:
    """What the extractor's calls produced since ``since`` cost, in the providers' currency."""
    row = store.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) AS spent FROM event_extractions WHERE produced_at >= ?",
        (stamp_text(since),),
    ).fetchone()
    return float(row["spent"])


def extractions(store: Store, *, as_of: date) -> tuple[EventExtraction, ...]:
    """Every extraction produced by the end of ``as_of``, in the order produced."""
    rows = store.execute(
        f"SELECT {_EXTRACTION_COLUMNS} FROM event_extractions WHERE produced_at < ? "
        "ORDER BY extraction_id",
        (cutoff_text(as_of),),
    ).fetchall()
    return tuple(_extraction_from_row(row) for row in rows)


def _extraction_from_row(row: sqlite3.Row) -> EventExtraction:
    return _read(
        "event_extractions",
        row,
        lambda: EventExtraction(
            item_id=row["item_id"],
            produced_at=read_stamp(row["produced_at"], "event_extractions", "produced_at"),
            provider=row["provider"],
            model_id=row["model_id"],
            prompt_version=row["prompt_version"],
            prompt_hash=row["prompt_hash"],
            status=ExtractionStatus(row["status"]),
            attempts=row["attempts"],
            output=row["output"],
            error=row["error"],
            input_tokens=row["input_tokens"],
            output_tokens=row["output_tokens"],
            cost_usd=row["cost_usd"],
        ),
    )


# ── earnings ──


def save_earnings_dates(store: Store, dates: Sequence[EarningsDate]) -> int:
    if not dates:
        return 0
    with store.transaction():
        store.executemany(
            "INSERT INTO earnings_dates (symbol, collected_on, report_at, source, grade) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT (symbol, collected_on, report_at) DO NOTHING",
            [
                (d.symbol, date_text(d.collected_on), stamp_text(d.report_at), d.source, d.grade)
                for d in dates
            ],
        )
    return len(dates)


def earnings_dates(store: Store, symbol: str, *, as_of: date) -> tuple[EarningsDate, ...]:
    """The dates the newest read held by ``as_of`` listed for ``symbol``, by report time."""
    rows = store.execute(
        "SELECT symbol, collected_on, report_at, source, grade FROM earnings_dates "
        "WHERE symbol = ? AND collected_on = (SELECT MAX(collected_on) FROM earnings_dates "
        "WHERE symbol = ? AND collected_on <= ?) ORDER BY report_at",
        (symbol, symbol, date_text(as_of)),
    ).fetchall()
    return tuple(_earnings_from_row(row) for row in rows)


def _earnings_from_row(row: sqlite3.Row) -> EarningsDate:
    return _read(
        "earnings_dates",
        row,
        lambda: EarningsDate(
            symbol=row["symbol"],
            collected_on=read_date(row["collected_on"], "earnings_dates", "collected_on"),
            report_at=read_stamp(row["report_at"], "earnings_dates", "report_at"),
            source=row["source"],
            grade=row["grade"],
        ),
    )


def earnings_collected_on(store: Store, symbol: str) -> date | None:
    """When ``symbol``'s earnings dates were last read."""
    row = store.execute(
        "SELECT MAX(collected_on) AS newest FROM earnings_dates WHERE symbol = ?", (symbol,)
    ).fetchone()
    return read_optional_date(row["newest"], "earnings_dates", "collected_on")


# ── halts ──


def save_halt_state(store: Store, origin: str, *, fetched_at: datetime, payload: str) -> None:
    """Keep a halt feed's last good answer (its incidents as JSON)."""
    with store.transaction():
        store.execute(
            "INSERT INTO halt_feed_state (origin, fetched_at, incidents) VALUES (?, ?, ?) "
            "ON CONFLICT (origin) DO UPDATE SET fetched_at = excluded.fetched_at, "
            "incidents = excluded.incidents",
            (origin, stamp_text(fetched_at), payload),
        )


def halt_state(store: Store, origin: str) -> tuple[datetime, str] | None:
    """A halt feed's last good answer and when it was fetched."""
    row = store.execute(
        "SELECT fetched_at, incidents FROM halt_feed_state WHERE origin = ?", (origin,)
    ).fetchone()
    if row is None:
        return None
    return read_stamp(row["fetched_at"], "halt_feed_state", "fetched_at"), row["incidents"]
