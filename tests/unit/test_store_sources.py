"""The sources' store: source health and targets, the as-of bounds of every read, the extractor's
queue and spend, the append-only extraction log, the halt feed's last state, and a corrupt row
read as an error, never as data.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from obliskiq.data.insiders.form4 import parse_submission
from obliskiq.domain.sources import (
    CotReport,
    EventExtraction,
    ExtractionStatus,
    GdeltDay,
    NewsItem,
)
from obliskiq.errors import StoreError
from obliskiq.store import Store
from obliskiq.store import sources as source_repo

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
NOW = datetime(2026, 10, 23, 21, 0, tzinfo=UTC)


# ── health and targets ──


def test_a_success_resets_the_failures_and_a_failure_keeps_the_last_success(store: Store) -> None:
    first = source_repo.record_source_run(store, "fred", at=NOW, status="ok", rows=10, detail=None)
    assert first.consecutive_failures == 0
    assert first.last_success_at == NOW
    later = NOW + timedelta(hours=6)
    failed = source_repo.record_source_run(
        store, "fred", at=later, status="error", rows=0, detail="down"
    )
    assert failed.consecutive_failures == 1
    assert failed.last_success_at == NOW
    again = source_repo.record_source_run(
        store, "fred", at=later + timedelta(hours=1), status="error", rows=0, detail="down"
    )
    assert again.consecutive_failures == 2
    never = source_repo.record_source_run(
        store, "kalshi", at=NOW, status="skipped", rows=0, detail="nothing to do"
    )
    assert never.last_success_at is None
    assert never.consecutive_failures == 1
    runs = source_repo.source_runs(store)
    assert list(runs) == ["fred", "kalshi"]
    assert runs["fred"] == again
    assert runs["fred"].detail == "down"
    degraded = source_repo.record_source_run(
        store, "fred", at=later + timedelta(hours=2), status="degraded", rows=1, detail=None
    )
    assert degraded.consecutive_failures == 0


def test_a_target_s_last_success_survives_a_failure_but_is_not_reported_as_fresh(
    store: Store,
) -> None:
    source_repo.record_target(store, "fred", "DGS10", at=NOW, ok=True, rows=3, detail=None)
    assert source_repo.target_succeeded_at(store, "fred", "DGS10") == NOW
    source_repo.record_target(
        store, "fred", "DGS10", at=NOW + timedelta(hours=1), ok=False, rows=0, detail="429"
    )
    assert source_repo.target_succeeded_at(store, "fred", "DGS10") is None
    assert source_repo.target_succeeded_at(store, "fred", "UNKNOWN") is None
    held = store.execute("SELECT last_success_at FROM source_targets").fetchone()
    assert held["last_success_at"] == "2026-10-23T21:00:00Z"
    source_repo.record_target(
        store, "sec_edgar", "index:2026-10-01", at=NOW, ok=True, rows=0, detail=None
    )
    source_repo.record_target(store, "sec_edgar", "other", at=NOW, ok=True, rows=0, detail=None)
    assert source_repo.succeeded_targets(store, "sec_edgar", "index:") == ("index:2026-10-01",)


# ── as-of bounds ──


def cot(report_day: date) -> CotReport:
    return CotReport(
        contract_code="088691",
        report_date=report_day,
        release_date=report_day + timedelta(days=3),
        market_name="GOLD",
        open_interest=1000,
        noncomm_long=600,
        noncomm_short=400,
        comm_long=None,
        comm_short=None,
        nonrept_long=None,
        nonrept_short=None,
        source="cftc_socrata",
        collected_at=NOW,
    )


def test_cot_reports_are_read_by_release_from_a_report_date(store: Store) -> None:
    assert source_repo.upsert_cot_reports(store, []) == 0
    source_repo.upsert_cot_reports(store, [cot(date(2026, 9, 29)), cot(date(2026, 10, 6))])
    since = source_repo.cot_reports(
        store, "088691", as_of=date(2026, 10, 9), since=date(2026, 10, 1)
    )
    assert [r.report_date for r in since] == [date(2026, 10, 6)]
    assert len(source_repo.cot_reports(store, "088691", as_of=date(2026, 10, 8))) == 1


def test_a_filing_is_stored_once_and_read_from_its_filing_date(store: Store) -> None:
    submission = (FIXTURES / "insiders" / "form4_purchase_mfm.txt").read_text(encoding="utf-8")
    filing = parse_submission(submission, zone=ZoneInfo("America/New_York"), collected_at=NOW)
    assert source_repo.save_insider_filing(store, filing)
    assert not source_repo.save_insider_filing(store, filing)
    cik = filing.issuer_cik
    assert source_repo.insider_filings(store, cik, as_of=date(2026, 10, 1), since=date(2026, 10, 1))
    assert not source_repo.insider_filings(
        store, cik, as_of=date(2026, 10, 9), since=date(2026, 10, 2)
    )


def item(name: str, collected: datetime) -> NewsItem:
    return NewsItem(
        item_id=name * 64,
        feed="fed_press",
        kind="central_bank",
        region="us",
        symbol=None,
        guid=None,
        link=None,
        title=f"Item {name}",
        summary=None,
        published_at=None,
        collected_at=collected,
        raw="<item/>",
        fallback_tone=None,
    )


def test_empty_saves_write_nothing_and_news_is_read_from_a_day(store: Store) -> None:
    assert source_repo.save_event_markets(store, [], seen_at=NOW) == 0
    assert source_repo.save_event_snapshots(store, []) == 0
    assert source_repo.save_gdelt_days(store, []) == 0
    assert source_repo.save_news_items(store, []) == 0
    assert source_repo.save_earnings_dates(store, []) == 0
    source_repo.save_news_items(store, [item("a", NOW - timedelta(days=2)), item("b", NOW)])
    recent = source_repo.news_items(store, as_of=NOW.date(), since=NOW.date())
    assert [i.title for i in recent] == ["Item b"]


# ── the extractor's queue, spend and log ──


def extraction(
    name: str, status: ExtractionStatus, *, at: datetime = NOW, cost: float = 0.001
) -> EventExtraction:
    return EventExtraction(
        item_id=name * 64,
        produced_at=at,
        provider="gemini",
        model_id="model",
        prompt_version=1,
        prompt_hash="f" * 64,
        status=status,
        attempts=1,
        output='{"type": "x"}' if status is ExtractionStatus.OK else None,
        error=None if status is ExtractionStatus.OK else "why",
        input_tokens=100,
        output_tokens=20,
        cost_usd=cost,
    )


def test_the_queue_holds_items_without_a_verdict_and_under_the_failure_cap(store: Store) -> None:
    source_repo.save_news_items(
        store, [item(name, NOW - timedelta(minutes=n)) for n, name in enumerate("abcd")]
    )
    source_repo.record_extraction(store, extraction("a", ExtractionStatus.OK))
    source_repo.record_extraction(store, extraction("b", ExtractionStatus.INVALID))
    source_repo.record_extraction(store, extraction("c", ExtractionStatus.FAILED))
    source_repo.record_extraction(store, extraction("c", ExtractionStatus.FAILED))
    source_repo.record_extraction(store, extraction("d", ExtractionStatus.FAILED))

    def pending(
        *, prompt_hash: str = "f" * 64, limit: int = 10, max_failures: int = 2
    ) -> list[str]:
        held = source_repo.pending_news_items(
            store, model_id="model", prompt_hash=prompt_hash, limit=limit, max_failures=max_failures
        )
        return [i.title for i in held]

    assert pending() == ["Item d"]
    assert pending(max_failures=3) == ["Item d", "Item c"]
    assert pending(prompt_hash="e" * 64) == ["Item d", "Item c", "Item b", "Item a"]
    assert pending(prompt_hash="e" * 64, limit=1) == ["Item d"]
    assert source_repo.extraction_spend(store, NOW) == pytest.approx(0.005)
    assert source_repo.extraction_spend(store, NOW + timedelta(seconds=1)) == 0
    held = source_repo.extractions(store, as_of=NOW.date())
    assert [e.status for e in held][:2] == [ExtractionStatus.OK, ExtractionStatus.INVALID]
    assert held[0].output == '{"type": "x"}'
    assert source_repo.extractions(store, as_of=NOW.date() - timedelta(days=1)) == ()


@pytest.mark.parametrize(
    "statement", ["UPDATE event_extractions SET cost_usd = 0", "DELETE FROM event_extractions"]
)
def test_the_extraction_log_is_append_only(store: Store, statement: str) -> None:
    source_repo.save_news_items(store, [item("a", NOW)])
    source_repo.record_extraction(store, extraction("a", ExtractionStatus.OK))
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        store.execute(statement)


# ── halts and corrupt rows ──


def test_the_halt_feed_s_last_state_round_trips(store: Store) -> None:
    assert source_repo.halt_state(store, "nasdaq-trader-halts") is None
    source_repo.save_halt_state(store, "nasdaq-trader-halts", fetched_at=NOW, payload="[]")
    later = NOW + timedelta(minutes=5)
    source_repo.save_halt_state(
        store, "nasdaq-trader-halts", fetched_at=later, payload='[{"a": 1}]'
    )
    assert source_repo.halt_state(store, "nasdaq-trader-halts") == (later, '[{"a": 1}]')


def test_a_row_that_cannot_be_read_is_an_error(store: Store) -> None:
    store.execute(
        "INSERT INTO event_markets (venue, market_id, event_id, question, outcome, closes_at, "
        "curated, tags, first_seen_at, last_seen_at) VALUES "
        "('kalshi', 'K', NULL, 'Q?', 'Yes', NULL, 1, 'not json', ?, ?)",
        ("2026-10-23T21:00:00Z", "2026-10-23T21:00:00Z"),
    )
    with pytest.raises(StoreError, match="event_markets row"):
        source_repo.event_markets(store, as_of=NOW.date())


def test_gdelt_days_keep_their_first_reading(store: Store) -> None:
    first = GdeltDay(
        query_id="q", day=date(2026, 10, 22), articles=5, monitored=10, tone=-1.0, collected_at=NOW
    )
    revised = first.model_copy(update={"articles": 7, "collected_at": NOW + timedelta(days=1)})
    assert source_repo.save_gdelt_days(store, [first]) == 1
    assert source_repo.save_gdelt_days(store, [revised]) == 0
    (held,) = source_repo.gdelt_days(store, "q", as_of=NOW.date() + timedelta(days=1))
    assert held.articles == 5
