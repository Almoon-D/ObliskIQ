"""The SEC's filings as events and as the dossier's text (Session 34; DECISIONS D189, D200): the
parsers on recorded answers, the collector end to end with EDGAR mocked, the sheet's events
section as of a date, a call's event tags in the idea ledger, and a candidate's report sections.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import json
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx

from obliskiq.config import ObliskConfig
from obliskiq.data.events.filing_parse import (
    html_text,
    parse_header,
    parse_prospectus,
    primary_document,
    report_sections,
)
from obliskiq.data.events.filing_text import (
    ANNUAL_SECTIONS,
    QUARTERLY_SECTIONS,
    CompanyTexts,
    chosen_filings,
    company_texts,
)
from obliskiq.data.events.filings import FilingsCollector
from obliskiq.data.market.plumbing import Pacer
from obliskiq.domain.filings import Filing, FilingText, IpoTerms, event_tags
from obliskiq.domain.stocks import StockBar, StockListing
from obliskiq.errors import StoreError, UpstreamError
from obliskiq.grid.events import event_view, events_summary
from obliskiq.grid.facts import build_fact_sheet
from obliskiq.store import Store
from obliskiq.store import filings as filings_repo
from obliskiq.store import sources as source_repo
from obliskiq.store import stocks as stocks_repo
from obliskiq.store.worker import StoreWorker
from obliskiq.util.http import RetryPolicy

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "filings"
NEW_YORK = ZoneInfo("America/New_York")
ARCHIVE = "https://www.sec.gov/Archives/edgar"
INDEX = f"{ARCHIVE}/daily-index/2026/QTR4/form.{{day}}.idx"
SUBMISSIONS = "https://data.sec.gov/submissions/CIK0001531152.json"
#: Friday 20 November 2026, after EDGAR posted the day's index.
NOW = datetime(2026, 11, 20, 23, 0, tzinfo=UTC)
BJ, AGILENT, ANTHROPIC, MEDLINE = 1531152, 1090872, 2130357, 2046386
ANT_ACCESSION = "0001628280-26-074944"
BJ_8K = "0001531152-26-000083"
AGILENT_8K = "0001193125-26-470278"


def text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def body(name: str) -> Any:
    return json.loads(text(name))


async def no_sleep(_seconds: float) -> None:
    return None


def fast(settings: ObliskConfig, **filings: Any) -> ObliskConfig:
    """The shipped settings without pauses or retries."""
    retry = settings.collectors.retry.model_copy(update={"target_retries": 0})
    sources = settings.sources
    return settings.model_copy(
        update={
            "collectors": settings.collectors.model_copy(update={"retry": retry}),
            "sources": sources.model_copy(
                update={"filings": sources.filings.model_copy(update=filings)}
            ),
        }
    )


def policy(settings: ObliskConfig) -> RetryPolicy:
    return RetryPolicy.from_settings(settings.http.model_copy(update={"retries": 0}))


def listing(cik: int, ticker: str, name: str) -> StockListing:
    return StockListing(cik=cik, ticker=ticker, name=name, exchange="NYSE", venue="XNYS")


LISTED = (
    listing(BJ, "BJ", "BJ's Wholesale Club Holdings"),
    listing(AGILENT, "A", "Agilent Technologies"),
    listing(ANTHROPIC, "ANT", "Anthropic"),
)


def seed_listings(store: Store, listings: Iterable[StockListing] = LISTED) -> None:
    at = datetime(2026, 10, 1, 22, 0, tzinfo=UTC)
    stocks_repo.save_snapshot(store, list(listings), known_at=at, content_sha256="0" * 64)


def seed_bars(store: Store, tickers: Iterable[str], *, until: date) -> None:
    """Thirty liquid sessions to ``until`` for each ticker: they pass the screen's floors."""
    days = [until - timedelta(days=n) for n in range(45)]
    sessions = sorted(d for d in days if d.weekday() < 5)[-30:]
    stocks_repo.save_bars(
        store,
        [
            StockBar(
                symbol=ticker,
                session=day,
                close=50.0,
                volume=1_000_000.0,
                change=0.001,
                known_at=datetime.combine(day, datetime.min.time(), tzinfo=UTC)
                + timedelta(hours=23),
            )
            for ticker in tickers
            for day in sessions
        ],
    )


# ── the parsers ──


def test_an_8k_header_states_its_items_and_its_acceptance_in_utc() -> None:
    items, accepted = parse_header(text("hdr_bj_8k_results.sgml"), zone=NEW_YORK)
    assert items == ("2.02", "9.01")
    assert accepted == datetime(2026, 11, 20, 12, 1, 48, tzinfo=UTC)
    assert parse_header(text("hdr_agilent_8k_departure.sgml"), zone=NEW_YORK)[0] == ("5.02",)
    assert parse_header("<SEC-HEADER>\n<TYPE>8-K\n", zone=NEW_YORK) == ((), None)
    with pytest.raises(UpstreamError, match="SEC-HEADER"):
        parse_header("<html>not a header</html>", zone=NEW_YORK)


def test_the_primary_document_is_the_largest_html_one() -> None:
    assert primary_document(body("index_anthropic_424b4.json")) == "anthropicpbc-424b4.htm"
    odd = {"directory": {"item": [{"name": "a.htm", "size": "x"}, {"name": "b.htm"}]}}
    assert primary_document(odd) == "a.htm"
    with pytest.raises(UpstreamError, match="no documents"):
        primary_document({"directory": {}})
    with pytest.raises(UpstreamError, match="no HTML"):
        primary_document({"directory": {"item": [{"name": "x.jpg", "size": "1"}]}})


def test_an_ipo_prospectus_states_its_price_shares_and_shortest_lock_up() -> None:
    read = parse_prospectus(html_text(text("prospectus_ipo_anthropic.htm")), cover_chars=6000)
    assert read.ipo
    assert read.price == Decimal("83.00")
    assert read.shares == 980_000_000
    # 180 and 365 days are stated for the lock-up; the dealers' 25 days are not a lock-up.
    assert read.lockup_days == 180
    assert read.prospectus_day == date(2026, 11, 18)
    assert read.lockup_ends(date(2026, 11, 19)) == date(2027, 5, 17)


def test_a_lock_up_the_prospectus_does_not_state_yields_no_date() -> None:
    whole = html_text(text("prospectus_ipo_anthropic.htm"))
    without = whole[: whole.index("Market standoff and lock-up agreements")]
    undated = without.replace("Prospectus dated November 18, 2026", "")
    read = parse_prospectus(undated, cover_chars=6000)
    assert read.ipo
    assert read.lockup_days is None
    assert read.lockup_ends(date(2026, 11, 19)) is None
    assert read.prospectus_day is None
    stated = parse_prospectus(
        f"{undated} The lock-up agreements last 90 days after the date of this prospectus.",
        cover_chars=6000,
    )
    # Counted from the filing day when the prospectus states no date of its own.
    assert stated.lockup_ends(date(2026, 11, 19)) == date(2027, 2, 17)


def test_a_follow_on_offering_is_not_an_ipo() -> None:
    read = parse_prospectus(html_text(text("prospectus_follow_on_medline.htm")), cover_chars=6000)
    assert not read.ipo
    assert (read.price, read.shares, read.lockup_days) == (None, None, None)
    bare = parse_prospectus("This is an initial public offering of shares.", cover_chars=100)
    assert bare.ipo
    assert (bare.price, bare.shares) == (None, None)
    # A run of commas is no number of shares.
    commas = parse_prospectus(
        "This is an initial public offering of ,,,,,, Shares.", cover_chars=100
    )
    assert commas.shares is None


def test_a_report_s_sections_are_the_longest_spans_after_their_headings() -> None:
    annual = report_sections(html_text(text("bj_10k_excerpt.htm")), ANNUAL_SECTIONS)
    business, risk, discussion = (annual[name] or "" for name in ANNUAL_SECTIONS)
    assert business.startswith("Item 1. Business General BJ")
    assert risk.startswith("Item 1A. Risk Factors Set forth below")
    assert discussion.startswith("Item 7. Management")
    # A cross-reference ("see “Item 1A. Risk Factors”") is no heading and no boundary.
    assert "Item 1B" not in risk
    quarterly = report_sections(html_text(text("bj_10q_excerpt.htm")), QUARTERLY_SECTIONS)
    assert (quarterly["management_discussion"] or "").startswith("Item 2. Management")
    assert (quarterly["risk_factors"] or "").startswith("Item 1A. Risk Factors. There have been")
    # Of two headings of one item, the longer span is the section.
    twice = report_sections(
        "Item 1. Business the long account of the company. Item 2. Properties none. "
        "Item 1. Business short.",
        {"business": ("1", "Business")},
    )
    assert twice == {"business": "Item 1. Business the long account of the company."}
    assert report_sections("no headings at all", {"business": ("1", "Business")}) == {
        "business": None
    }


# ── the collector ──


def collector(
    settings: ObliskConfig, worker: StoreWorker, client: httpx.AsyncClient, **changes: Any
) -> FilingsCollector:
    return FilingsCollector(
        settings,
        client=client,
        policy=policy(settings),
        worker=worker,
        pacer=Pacer(settings.collectors, no_sleep),
        user_agent=changes.get("user_agent", "research test@example.com"),
        now=changes.get("now", lambda: NOW),
        sleep=no_sleep,
    )


def edgar(router: respx.MockRouter, *, prospectus: httpx.Response | None = None) -> None:
    router.get(INDEX.format(day="20261119")).mock(
        return_value=httpx.Response(200, text=text("form_index_20261119.idx"))
    )
    router.get(INDEX.format(day="20261120")).mock(
        return_value=httpx.Response(200, text=text("form_index_20261120.idx"))
    )
    router.get(f"{ARCHIVE}/data/{BJ}/000153115226000083/{BJ_8K}.hdr.sgml").mock(
        return_value=httpx.Response(200, text=text("hdr_bj_8k_results.sgml"))
    )
    router.get(f"{ARCHIVE}/data/{AGILENT}/000119312526470278/{AGILENT_8K}.hdr.sgml").mock(
        return_value=httpx.Response(200, text=text("hdr_agilent_8k_departure.sgml"))
    )
    folder = f"{ARCHIVE}/data/{ANTHROPIC}/000162828026074944"
    router.get(f"{folder}/index.json").mock(
        return_value=httpx.Response(200, json=body("index_anthropic_424b4.json"))
    )
    router.get(f"{folder}/anthropicpbc-424b4.htm").mock(
        return_value=prospectus or httpx.Response(200, text=text("prospectus_ipo_anthropic.htm"))
    )


async def test_a_run_keeps_the_listed_names_8ks_and_every_registration(
    shipped_settings: ObliskConfig, store: Store, store_worker: StoreWorker
) -> None:
    settings = fast(shipped_settings, lookback_days=1)
    seed_listings(store)
    with respx.mock(assert_all_called=True) as router:
        edgar(router)
        async with httpx.AsyncClient() as client:
            collection = await collector(settings, store_worker, client).run()
    assert collection.status == "ok", collection.judge().reason
    held = {
        f.accession: f
        for f in filings_repo.filings(store, as_of=NOW.date(), since=NOW.date() - timedelta(days=5))
    }
    forms = sorted((f.form, f.cik) for f in held.values())
    # AMD's 8-K is not of a listed name; the Form 4 and the 10-K are not kept forms.
    assert forms == [
        ("424B4", MEDLINE),
        ("424B4", ANTHROPIC),
        ("8-K", AGILENT),
        ("8-K", BJ),
        ("F-1", 1998560),
        ("S-1", 2136881),
    ]
    assert held[BJ_8K].items == ("2.02", "9.01")
    assert held[BJ_8K].accepted_at == datetime(2026, 11, 20, 12, 1, 48, tzinfo=UTC)
    assert held[BJ_8K].company == "BJ's Wholesale Club Holdings, Inc."
    assert held[ANT_ACCESSION].items is None
    # The listed issuer's prospectus is read; Medline's (not listed here) waits.
    terms = filings_repo.ipo_terms(store, as_of=NOW.date())
    assert list(terms) == [ANT_ACCESSION]
    assert terms[ANT_ACCESSION].lockup_ends == date(2027, 5, 17)
    assert terms[ANT_ACCESSION].price == Decimal("83.00")
    assert collection.notes["listed"] == 3
    # A second run reads no index it has read and fetches no filing it holds.
    with respx.mock(assert_all_called=False) as router:
        edgar(router)
        async with httpx.AsyncClient() as client:
            again = await collector(settings, store_worker, client).run()
        assert router.calls.call_count == 0
    assert again.status == "skipped"


async def test_a_filing_already_held_is_not_asked_again(
    shipped_settings: ObliskConfig, store: Store, store_worker: StoreWorker
) -> None:
    settings = fast(shipped_settings, lookback_days=1)
    seed_listings(store)
    held = Filing(
        accession=AGILENT_8K,
        cik=AGILENT,
        company="AGILENT TECHNOLOGIES, INC.",
        form="8-K",
        filed=date(2026, 11, 20),
        path="edgar/data/1090872/0001193125-26-470278.txt",
        items=("5.02",),
        known_at=NOW,
    )
    filings_repo.save_filings(store, [held])
    with respx.mock(assert_all_called=False) as router:
        edgar(router)
        async with httpx.AsyncClient() as client:
            await collector(settings, store_worker, client).run()
        asked = [call.request.url.path for call in router.calls]
    assert not any(AGILENT_8K in path for path in asked)
    assert any(BJ_8K in path for path in asked)


async def test_a_closed_day_a_failed_header_and_a_failed_prospectus(
    shipped_settings: ObliskConfig, store: Store, store_worker: StoreWorker
) -> None:
    settings = fast(shipped_settings, lookback_days=1)
    seed_listings(store)
    closed = httpx.Response(403, text="<Error><Code>AccessDenied</Code></Error>")
    with respx.mock(assert_all_called=False) as router:
        edgar(router, prospectus=httpx.Response(500, text="down"))
        router.get(INDEX.format(day="20261119")).mock(return_value=closed)
        router.get(f"{ARCHIVE}/data/{BJ}/000153115226000083/{BJ_8K}.hdr.sgml").mock(
            return_value=httpx.Response(404, text="gone")
        )
        async with httpx.AsyncClient() as client:
            collection = await collector(settings, store_worker, client).run()
    assert collection.status == "degraded"
    reason = collection.judge().reason
    assert BJ_8K in reason
    held = filings_repo.held_accessions(store, [BJ_8K, AGILENT_8K])
    assert held == {AGILENT_8K}
    targets = source_repo.succeeded_targets(store, "sec_filings", "index:")
    # The closed day is recorded as read with nothing; the failed prospectus is asked again.
    assert targets == ("index:2026-11-19", "index:2026-11-20")
    assert filings_repo.ipo_terms(store, as_of=NOW.date()) == {}
    unread = filings_repo.unread_prospectuses(
        store, forms=["424B4"], ciks={ANTHROPIC}, since=date(2026, 11, 1), limit=5
    )
    assert [f.accession for f in unread] == []  # the 19th was closed: nothing was listed


async def test_today_s_absent_index_is_asked_again_and_a_dry_run_records_nothing(
    shipped_settings: ObliskConfig, store: Store, store_worker: StoreWorker
) -> None:
    settings = fast(shipped_settings, lookback_days=0)
    closed = httpx.Response(403, text="<Error><Code>AccessDenied</Code></Error>")
    with respx.mock(assert_all_called=True) as router:
        router.get(INDEX.format(day="20261120")).mock(return_value=closed)
        async with httpx.AsyncClient() as client:
            collection = await collector(settings, store_worker, client).run()
    assert collection.status == "skipped"
    assert source_repo.succeeded_targets(store, "sec_filings", "index:") == ()
    with respx.mock(assert_all_called=True) as router:
        router.get(INDEX.format(day="20261120")).mock(
            return_value=httpx.Response(200, text=text("form_index_20261120.idx"))
        )
        async with httpx.AsyncClient() as client:
            dry = await collector(settings, store_worker, client).run(dry_run=True)
    # No snapshot is held: no 8-K is followed, the registrations are listed from the index.
    assert dry.status == "ok"
    assert dry.rows == 1
    assert filings_repo.held_accessions(store, ["0001213900-26-122381"]) == set()
    with respx.mock(assert_all_called=True) as router:
        router.get(INDEX.format(day="20261120")).mock(return_value=httpx.Response(500))
        async with httpx.AsyncClient() as client:
            broken = await collector(settings, store_worker, client).run()
    assert broken.status == "error"


async def test_a_follow_on_prospectus_is_read_as_not_an_ipo_and_a_huge_one_is_refused(
    shipped_settings: ObliskConfig, store: Store, store_worker: StoreWorker
) -> None:
    settings = fast(shipped_settings, lookback_days=1)
    seed_listings(store)
    medline = httpx.Response(200, text=text("prospectus_follow_on_medline.htm"))
    with respx.mock(assert_all_called=False) as router:
        edgar(router, prospectus=medline)
        async with httpx.AsyncClient() as client:
            await collector(settings, store_worker, client).run()
    read = store.execute("SELECT status, detail FROM ipo_terms").fetchall()
    assert [tuple(row) for row in read] == [
        ("not_ipo", "its cover does not say it is an initial public offering")
    ]
    tight = fast(shipped_settings, lookback_days=1, max_document_bytes=10)
    with respx.mock(assert_all_called=False) as router:
        edgar(router)
        router.get(f"{ARCHIVE}/data/{ANTHROPIC}/000162828026074944/index.json").mock(
            return_value=httpx.Response(404)
        )
        async with httpx.AsyncClient() as client:
            seeded = await collector(tight, store_worker, client).run()
    assert seeded.status == "skipped"  # nothing new: every day held, every prospectus read


async def test_a_prospectus_s_documents_that_fail_are_named(
    shipped_settings: ObliskConfig, store: Store, store_worker: StoreWorker
) -> None:
    seed_listings(store)
    filing = Filing(
        accession=ANT_ACCESSION,
        cik=ANTHROPIC,
        company="Anthropic, PBC",
        form="424B4",
        filed=date(2026, 11, 19),
        path="edgar/data/2130357/0001628280-26-074944.txt",
        known_at=NOW,
    )
    filings_repo.save_filings(store, [filing])
    source_repo.record_target(
        store, "sec_filings", "index:2026-11-20", at=NOW, ok=True, rows=0, detail=None
    )
    folder = f"{ARCHIVE}/data/{ANTHROPIC}/000162828026074944"
    for answer, big, expected in (
        (httpx.Response(404), False, "its documents answered HTTP 404"),
        (None, True, "is larger than 10 bytes"),
        (None, False, "answered HTTP 503"),
    ):
        settings = fast(shipped_settings, max_document_bytes=10 if big else 40_000_000)
        with respx.mock(assert_all_called=False) as router:
            router.get(f"{folder}/index.json").mock(
                return_value=answer or httpx.Response(200, json=body("index_anthropic_424b4.json"))
            )
            router.get(f"{folder}/anthropicpbc-424b4.htm").mock(
                return_value=httpx.Response(200, text=text("prospectus_ipo_anthropic.htm"))
                if big
                else httpx.Response(503)
            )
            async with httpx.AsyncClient() as client:
                collection = await collector(settings, store_worker, client).run()
        assert expected in collection.judge().reason


async def test_without_a_user_agent_nothing_is_asked(
    shipped_settings: ObliskConfig, store_worker: StoreWorker
) -> None:
    settings = fast(shipped_settings, lookback_days=0)
    with respx.mock(assert_all_called=False) as router:
        async with httpx.AsyncClient() as client:
            collection = await collector(settings, store_worker, client, user_agent=None).run()
        assert router.calls.call_count == 0
    assert collection.status == "error"
    assert "SEC_USER_AGENT" in collection.judge().reason


# ── the store ──


def test_the_filings_tables_are_append_only_and_read_as_of(store: Store) -> None:
    filing = Filing(
        accession=BJ_8K,
        cik=BJ,
        company="BJ's",
        form="8-K",
        filed=date(2026, 11, 20),
        path="edgar/data/1531152/0001531152-26-000083.txt",
        items=("2.02",),
        known_at=NOW,
    )
    assert filings_repo.save_filings(store, [filing, filing]) == 1
    assert filings_repo.filings(store, as_of=date(2026, 11, 19), since=date(2026, 11, 1)) == []
    assert filings_repo.held_accessions(store, []) == set()
    with pytest.raises(Exception, match="append-only"):
        store.execute("DELETE FROM sec_filings")
    store.execute(
        "INSERT INTO ipo_terms (accession, cik, status, price, known_at) "
        "VALUES (?, ?, 'ipo', 'abc', '2026-11-20T23:00:00Z')",
        (BJ_8K, BJ),
    )
    with pytest.raises(StoreError, match="cannot be read"):
        filings_repo.ipo_terms(store, as_of=NOW.date())


def test_the_records_hold_together() -> None:
    with pytest.raises(ValueError, match="lock-up's end"):
        IpoTerms(accession="a", cik=1, status="ipo", lockup_days=180, known_at=NOW)
    with pytest.raises(ValueError, match="only an initial public offering"):
        IpoTerms(accession="a", cik=1, status="not_ipo", price=Decimal(1), detail="x", known_at=NOW)
    with pytest.raises(ValueError, match="says why"):
        IpoTerms(accession="a", cik=1, status="failed", known_at=NOW)
    common: dict[str, Any] = {
        "accession": "a",
        "cik": 1,
        "form": "10-K",
        "filed": date(2026, 3, 12),
        "section": "business",
        "known_at": NOW,
    }
    with pytest.raises(ValueError, match="its text"):
        FilingText(**common, status="ok")
    with pytest.raises(ValueError, match="says why"):
        FilingText(**common, status="not_found")


# ── the sheet ──


def seed_events(store: Store, *, known: datetime = NOW) -> None:
    seed_listings(store)
    seed_bars(store, ["BJ", "A", "ANT"], until=date(2026, 11, 20))
    filings_repo.save_filings(
        store,
        [
            Filing(
                accession=BJ_8K,
                cik=BJ,
                company="BJ's Wholesale Club Holdings, Inc.",
                form="8-K",
                filed=date(2026, 11, 20),
                path="edgar/data/1531152/0001531152-26-000083.txt",
                items=("2.02", "9.01"),
                known_at=known,
            ),
            Filing(
                accession=AGILENT_8K,
                cik=AGILENT,
                company="AGILENT TECHNOLOGIES, INC.",
                form="8-K",
                filed=date(2026, 11, 20),
                path="edgar/data/1090872/0001193125-26-470278.txt",
                items=("5.02",),
                known_at=known,
            ),
            Filing(
                accession=ANT_ACCESSION,
                cik=ANTHROPIC,
                company="Anthropic, PBC",
                form="424B4",
                filed=date(2026, 11, 19),
                path="edgar/data/2130357/0001628280-26-074944.txt",
                known_at=known,
            ),
            Filing(
                accession="0002130357-26-000001",
                cik=ANTHROPIC,
                company="Anthropic, PBC",
                form="S-1",
                filed=date(2026, 10, 30),
                path="edgar/data/2130357/0002130357-26-000001.txt",
                known_at=known,
            ),
            Filing(
                accession="0001998560-26-000001",
                cik=1998560,
                company="Axonex AI Group Ltd",
                form="F-1",
                filed=date(2026, 11, 20),
                path="edgar/data/1998560/0001998560-26-000001.txt",
                known_at=known,
            ),
        ],
    )
    filings_repo.save_ipo_terms(
        store,
        IpoTerms(
            accession=ANT_ACCESSION,
            cik=ANTHROPIC,
            status="ipo",
            price=Decimal("83.00"),
            shares=980_000_000,
            lockup_days=180,
            prospectus_day=date(2026, 11, 18),
            lockup_ends=date(2027, 5, 17),
            known_at=known,
        ),
    )


def test_the_sheet_carries_the_listed_names_events_by_kind(
    shipped_settings: ObliskConfig, store: Store
) -> None:
    seed_events(store)
    sheet = build_fact_sheet(store, shipped_settings, as_of=NOW.date())
    lines = [line for line in sheet.lines if line.section == "events"]
    assert "no signal" in lines[0].text
    assert lines[0].text.endswith(
        "3 names pass the floors, 4 events filed in the last 30 days or ending in the next 30"
    )
    assert [line.status for line in lines[1:3]] == ["no_source", "no_source"]
    assert lines[1].text.startswith("results_announced: ")
    kinds = [line.text.split(":")[0] for line in lines[3:]]
    # Newest first; the 8-K's exhibit item 9.01 is no event; the unlisted F-1 is not shown.
    assert kinds == ["results", "departure", "ipo_priced", "ipo_registration"]
    assert (
        "initial public offering at 83.00 a share, 980000000 shares offered, shortest lock-up "
        "stated 180 days, ends 2027-05-17"
    ) in lines[5].text
    assert lines[3].day == date(2026, 11, 20)
    # A name on an event line may be a thesis's stock, at its last close (D195).
    assert set(sheet.stocks) == {"BJ", "A", "ANT"}
    assert sheet.stock_closes["BJ"] == 50.0


def test_a_lock_up_ending_in_the_window_is_a_line_dated_by_its_prospectus(
    shipped_settings: ObliskConfig, store: Store
) -> None:
    seed_events(store)
    seed_bars(store, ["BJ", "A", "ANT"], until=date(2027, 5, 7))
    view = event_view(store, shipped_settings, as_of=date(2027, 5, 7))
    assert [line.kind for line in view.lines] == ["lockup_end"]
    assert view.ahead == 1
    assert view.lines[0].day == date(2026, 11, 18)
    assert "(180 days, stated) ends 2027-05-17" in view.lines[0].text
    summary = events_summary(store, shipped_settings, as_of=date(2027, 5, 7))
    assert summary.startswith("events: 0 filed in the last 30 days and 1 ahead in the next 30")
    assert "no_source: results_announced, index_change" in summary


def test_rows_known_after_the_date_change_neither_the_sheet_nor_its_events(
    shipped_settings: ObliskConfig, store: Store, tmp_path: Path, open_test_store: Any
) -> None:
    seed_events(store)
    before = build_fact_sheet(store, shipped_settings, as_of=NOW.date()).sheet_hash
    later = NOW + timedelta(days=1)
    filings_repo.save_filings(
        store,
        [
            Filing(
                accession="0001531152-26-000099",
                cik=BJ,
                company="BJ's",
                form="8-K",
                filed=date(2026, 11, 20),
                path="edgar/data/1531152/0001531152-26-000099.txt",
                items=("1.01",),
                known_at=later,
            )
        ],
    )
    store.execute(
        "INSERT INTO ipo_terms (accession, cik, status, known_at, detail) "
        "VALUES ('0001531152-26-000099', ?, 'not_ipo', ?, 'x')",
        (BJ, later.strftime("%Y-%m-%dT%H:%M:%SZ")),
    )
    assert build_fact_sheet(store, shipped_settings, as_of=NOW.date()).sheet_hash == before


def test_a_prospectus_not_read_yet_is_no_line(shipped_settings: ObliskConfig, store: Store) -> None:
    seed_listings(store)
    seed_bars(store, ["BJ"], until=date(2026, 11, 20))
    unread = Filing(
        accession="0001531152-26-000099",
        cik=BJ,
        company="BJ's Wholesale Club Holdings, Inc.",
        form="424B4",
        filed=date(2026, 11, 20),
        path="edgar/data/1531152/0001531152-26-000099.txt",
        known_at=NOW,
    )
    filings_repo.save_filings(store, [unread])
    assert event_view(store, shipped_settings, as_of=NOW.date()).lines == ()


def test_without_names_passing_the_floors_the_events_are_a_status_line(
    shipped_settings: ObliskConfig, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    sheet = build_fact_sheet(store, shipped_settings, as_of=NOW.date())
    lines = [line for line in sheet.lines if line.section == "events"]
    assert lines[0].status == "n/a"
    assert lines[0].text.endswith("no listed name passes the floors")

    def broken(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("unreadable")

    monkeypatch.setattr("obliskiq.grid.facts.event_view", broken)
    failed = build_fact_sheet(store, shipped_settings, as_of=NOW.date())
    lines = [line for line in failed.lines if line.section == "events"]
    assert [(line.status, line.text.split("; ")[-1]) for line in lines] == [
        ("failed", "they cannot be built: RuntimeError")
    ]


def test_a_call_s_event_tags_are_the_kinds_of_the_event_lines_it_cites(
    shipped_settings: ObliskConfig,
) -> None:
    kinds = shipped_settings.sources.filings.kinds()
    rendered = [
        "F3 | 2026-11-20 | ok | events | results: BJ (BJ's) 8-K filed 2026-11-20, item 2.02",
        "F4 | 2026-11-20 | no_source | events | index_change: no free source states it",
        "F5 | 2026-11-20 | ok | macro | results: not an event line",
        "F6 | 2026-11-20 | ok | events | results: BJ again",
        "F7 | 2026-11-19 | ok | events | ipo_priced: ANT ...",
        "a line of another shape",
    ]
    assert event_tags(rendered, kinds) == ("results", "ipo_priced")
    assert event_tags([], kinds) == ()


# ── the dossier's text ──


def texts_reader(
    settings: ObliskConfig, worker: StoreWorker, client: httpx.AsyncClient, now: datetime
) -> CompanyTexts:
    return company_texts(
        settings,
        client=client,
        worker=worker,
        secrets=lambda _name: "research test@example.com",
        now=lambda: now,
        sleep=no_sleep,
    )


def bj_documents(router: respx.MockRouter) -> None:
    folder = f"{ARCHIVE}/data/{BJ}"
    router.get(SUBMISSIONS).mock(return_value=httpx.Response(200, json=body("submissions_bj.json")))
    router.get(f"{folder}/000153115226000007/bj-20260131.htm").mock(
        return_value=httpx.Response(200, text=text("bj_10k_excerpt.htm"))
    )
    router.get(f"{folder}/000153115226000086/bj-20261031.htm").mock(
        return_value=httpx.Response(200, text=text("bj_10q_excerpt.htm"))
    )
    router.get(f"{folder}/000153115226000083/bj-20261120.htm").mock(
        return_value=httpx.Response(200, text=text("bj_8k_results.htm"))
    )
    router.get(f"{ARCHIVE}/data/{BJ}/000110465926116256/tm2627448d1_8k.htm").mock(
        return_value=httpx.Response(503)
    )


def test_the_reports_chosen_are_the_latest_annual_quarterly_and_recent_current_ones() -> None:
    recent = body("submissions_bj.json")["filings"]["recent"]
    rows = [
        {"form": f, "accessionNumber": a, "filingDate": d, "primaryDocument": p}
        for f, a, d, p in zip(
            recent["form"],
            recent["accessionNumber"],
            recent["filingDate"],
            recent["primaryDocument"],
            strict=True,
        )
    ]
    chosen = chosen_filings(
        rows,
        as_of=date(2026, 11, 22),
        annual="10-K",
        quarterly="10-Q",
        current="8-K",
        since=date(2026, 8, 24),
        most=5,
    )
    # The 10-Q of the 25th is after the date, and the file holds no earlier one.
    assert [(r["form"], r["filingDate"]) for r in chosen] == [
        ("10-K", "2026-03-12"),
        ("8-K", "2026-11-20"),
        ("8-K", "2026-10-15"),
    ]


async def test_a_candidate_s_report_sections_are_read_once_and_kept_as_read(
    shipped_settings: ObliskConfig, store: Store, store_worker: StoreWorker
) -> None:
    settings = fast(shipped_settings)
    seed_listings(store)
    today = datetime(2026, 11, 26, 22, 0, tzinfo=UTC)
    with respx.mock(assert_all_called=True) as router:
        bj_documents(router)
        async with httpx.AsyncClient() as client:
            lines = await texts_reader(settings, store_worker, client, today)("BJ", today.date())
    heads = [line.split(": ")[0] for line in lines]
    assert heads == [
        "10-K filed 2026-03-12 (0001531152-26-000007), business",
        "10-K filed 2026-03-12 (0001531152-26-000007), management_discussion",
        "10-K filed 2026-03-12 (0001531152-26-000007), risk_factors",
        "10-Q filed 2026-11-25 (0001531152-26-000086), management_discussion",
        "10-Q filed 2026-11-25 (0001531152-26-000086), risk_factors",
        "8-K filed 2026-11-20 (0001531152-26-000083), body",
        "8-K filed 2026-10-15 (0001104659-26-116256)",
    ]
    assert lines[-1].endswith("failed (UpstreamError: tm2627448d1_8k.htm answered HTTP 503)")
    cut = settings.llm.analyst.dossier_text.max_chars
    assert all(len(line) <= cut + 200 for line in lines)
    assert "Item 2.02 Results of Operations" in lines[5]
    # A second dossier the same day fetches only what failed.
    with respx.mock(assert_all_called=False) as router:
        bj_documents(router)
        async with httpx.AsyncClient() as client:
            await texts_reader(settings, store_worker, client, today)("BJ", today.date())
        asked = {call.request.url.path.rsplit("/", 1)[-1] for call in router.calls}
        assert asked == {"CIK0001531152.json", "tm2627448d1_8k.htm"}


async def test_a_dossier_of_an_earlier_day_reads_only_what_was_kept_by_it(
    shipped_settings: ObliskConfig, store: Store, store_worker: StoreWorker
) -> None:
    settings = fast(shipped_settings)
    seed_listings(store)
    today = datetime(2026, 11, 26, 22, 0, tzinfo=UTC)
    with respx.mock(assert_all_called=False) as router:
        async with httpx.AsyncClient() as client:
            before = await texts_reader(settings, store_worker, client, today)(
                "BJ", date(2026, 11, 25)
            )
        assert router.calls.call_count == 0
    assert before == ["BJ: no report of the company was read by 2026-11-25"]
    with respx.mock(assert_all_called=False) as router:
        bj_documents(router)
        async with httpx.AsyncClient() as client:
            await texts_reader(settings, store_worker, client, today)("BJ", today.date())
            # Read on the 26th: a dossier of the 25th still sees none of it.
            again = await texts_reader(settings, store_worker, client, today)(
                "BJ", date(2026, 11, 25)
            )
    assert again == before


async def test_a_failed_submissions_file_and_an_unknown_name_are_status_lines(
    shipped_settings: ObliskConfig, store: Store, store_worker: StoreWorker
) -> None:
    settings = fast(shipped_settings)
    seed_listings(store)
    today = datetime(2026, 11, 26, 22, 0, tzinfo=UTC)
    with respx.mock(assert_all_called=True) as router:
        router.get(SUBMISSIONS).mock(return_value=httpx.Response(500))
        async with httpx.AsyncClient() as client:
            reader = texts_reader(settings, store_worker, client, today)
            failed = await reader("BJ", today.date())
            unknown = await reader("EMIM", today.date())
    assert failed == [
        "the SEC's submissions file of CIK 1531152: failed (UpstreamError: answered HTTP 500)"
    ]
    assert unknown == ["EMIM: no filings read, it is not a listed company the SEC's file names"]
    with respx.mock(assert_all_called=True) as router:
        router.get(SUBMISSIONS).mock(return_value=httpx.Response(200, json={"filings": {}}))
        async with httpx.AsyncClient() as client:
            shapeless = await texts_reader(settings, store_worker, client, today)(
                "BJ", today.date()
            )
    assert "lists no recent filings" in shapeless[0]


async def test_a_report_without_a_heading_is_not_found_and_a_huge_one_fails(
    shipped_settings: ObliskConfig, store: Store, store_worker: StoreWorker
) -> None:
    seed_listings(store)
    today = datetime(2026, 11, 26, 22, 0, tzinfo=UTC)
    settings = fast(shipped_settings)
    tight = settings.model_copy(
        update={
            "sources": settings.sources.model_copy(
                update={
                    "filings": settings.sources.filings.model_copy(
                        update={"max_document_bytes": 10}
                    )
                }
            )
        }
    )
    with respx.mock(assert_all_called=False) as router:
        bj_documents(router)
        async with httpx.AsyncClient() as client:
            lines = await texts_reader(tight, store_worker, client, today)("BJ", today.date())
    assert any("is larger than 10 bytes" in line for line in lines)
    assert not any(": ok" in line or "not_found" in line for line in lines)
    with respx.mock(assert_all_called=False) as router:
        bj_documents(router)
        router.get(f"{ARCHIVE}/data/{BJ}/000153115226000007/bj-20260131.htm").mock(
            return_value=httpx.Response(200, text="<p>no items here</p>")
        )
        async with httpx.AsyncClient() as client:
            lines = await texts_reader(settings, store_worker, client, today)("BJ", today.date())
    assert (
        "10-K filed 2026-03-12 (0001531152-26-000007), business: not_found (bj-20260131.htm "
        "holds no heading of it)"
    ) in lines
