"""The stock collector (listings, fundamentals, bars) on recorded SEC and Yahoo answers, and the
stock screen view as of a date.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import csv
import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from obliskiq.config import ObliskConfig
from obliskiq.data.market.plumbing import Pacer
from obliskiq.data.stocks.bars import BarsCollector, chain, read_order
from obliskiq.data.stocks.fundamentals import FundamentalsCollector, frame_periods, parse_frame
from obliskiq.data.stocks.listings import ListingsCollector, parse_listings, parse_sic
from obliskiq.domain.stocks import StockBar, StockFact, StockListing
from obliskiq.errors import StoreError, UpstreamError
from obliskiq.grid.stock_screen import (
    ScreenStatus,
    months_before,
    percentile_ranks,
    screen_summary,
    stock_screen,
    window_return,
)
from obliskiq.research.data import yahoo_sessions
from obliskiq.store import Store
from obliskiq.store import sources as source_repo
from obliskiq.store import stocks as stocks_repo
from obliskiq.store.stocks import HeldBar
from obliskiq.store.worker import StoreWorker
from obliskiq.util.http import RetryPolicy

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "stocks"
TICKERS = "https://www.sec.gov/files/company_tickers_exchange.json"
SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
FRAMES = "https://data.sec.gov/api/xbrl/frames/"
DAY1 = datetime(2026, 11, 17, 21, 30, tzinfo=UTC)
DAY2 = DAY1 + timedelta(days=1)


def body(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


async def no_sleep(_seconds: float) -> None:
    return None


def fast(settings: ObliskConfig, **bars: Any) -> ObliskConfig:
    """The shipped settings without pauses or retries."""
    stocks = settings.sources.stocks
    retry = settings.collectors.retry.model_copy(update={"target_retries": 0})
    return settings.model_copy(
        update={
            "collectors": settings.collectors.model_copy(update={"retry": retry}),
            "sources": settings.sources.model_copy(
                update={
                    "stocks": stocks.model_copy(
                        update={"bars": stocks.bars.model_copy(update=bars)}
                    )
                }
            ),
        }
    )


def policy(settings: ObliskConfig) -> RetryPolicy:
    return RetryPolicy.from_settings(settings.http.model_copy(update={"retries": 0}))


# ── listings ──


def test_the_common_stocks_on_the_kept_exchanges_are_listed(
    shipped_settings: ObliskConfig,
) -> None:
    found = parse_listings(
        body("sec_company_tickers_exchange.json"), shipped_settings.sources.stocks.listings
    )
    tickers = {listing.ticker for listing in found}
    assert {"AAPL", "BRK-B", "BRK-A", "GOOG", "GOOGL", "JPM", "SPY"} <= tickers
    # preferreds, warrants, units, OTC, Cboe and an exchange the SEC leaves blank are not
    for gone in (
        "BAC-PB",
        "JPM-PC",
        "ETI-P",
        "VAL-WT",
        "KCAC-UN",
        "PSNYW",
        "BACRP",
        "CBOE",
        "INBT",
    ):
        assert gone not in tickers
    apple = next(listing for listing in found if listing.ticker == "AAPL")
    assert (apple.cik, apple.exchange, apple.venue) == (320193, "Nasdaq", "XNAS")
    with pytest.raises(UpstreamError, match="fields"):
        parse_listings(
            {"fields": ["cik", "ticker"], "data": []}, shipped_settings.sources.stocks.listings
        )


def test_a_company_s_sic_is_read_and_none_is_none() -> None:
    assert parse_sic(body("sec_submissions_aapl.json")) == 3571
    assert parse_sic(body("sec_submissions_spy.json")) is None
    with pytest.raises(UpstreamError):
        parse_sic({"name": "x"})


def listings_collector(
    settings: ObliskConfig, client: httpx.AsyncClient, worker: StoreWorker, now: datetime
) -> ListingsCollector:
    return ListingsCollector(
        settings,
        client=client,
        policy=policy(settings),
        worker=worker,
        user_agent="test agent",
        sleep=no_sleep,
        now=lambda: now,
    )


@respx.mock
async def test_a_name_that_leaves_is_recorded_and_stays_readable_before(
    shipped_settings: ObliskConfig, store_worker: StoreWorker, store: Store
) -> None:
    full = body("sec_company_tickers_exchange.json")
    first = parse_listings(full, shipped_settings.sources.stocks.listings)
    without_spy = {**full, "data": [row for row in full["data"] if row[2] != "SPY"]}
    route = respx.get(TICKERS).mock(
        side_effect=[httpx.Response(200, json=full), httpx.Response(200, json=without_spy)]
    )
    respx.get(url__regex=r"https://data\.sec\.gov/submissions/.*").mock(
        return_value=httpx.Response(200, json=body("sec_submissions_aapl.json"))
    )
    # Fourteen names: one leaving is more than the shipped share a snapshot may lose.
    stocks = shipped_settings.sources.stocks
    listings = stocks.listings.model_copy(update={"min_kept_share": Decimal("0.5")})
    settings = shipped_settings.model_copy(
        update={
            "sources": shipped_settings.sources.model_copy(
                update={"stocks": stocks.model_copy(update={"listings": listings})}
            )
        }
    )
    async with httpx.AsyncClient() as client:
        one = await listings_collector(settings, client, store_worker, DAY1).run()
        two = await listings_collector(settings, client, store_worker, DAY2).run()
    assert route.call_count == 2
    assert one.notes == {"listed": len(first), "left_listing": 0}
    assert two.notes == {"listed": len(first) - 1, "left_listing": 1}
    (left,) = stocks_repo.left_listings(store, as_of=DAY2.date())
    assert (left.ticker, left.last_seen_at, left.left_at) == ("SPY", DAY1, DAY2)
    # Read as of the first day, SPY is listed; as of the second, it is not; no row was deleted.
    earlier = stocks_repo.snapshot_as_of(store, as_of=DAY1.date())
    later = stocks_repo.snapshot_as_of(store, as_of=DAY2.date())
    assert earlier is not None
    assert later is not None
    assert "SPY" in {s.ticker for s in stocks_repo.listed(store, earlier)}
    assert "SPY" not in {s.ticker for s in stocks_repo.listed(store, later)}
    assert stocks_repo.left_listings(store, as_of=DAY1.date()) == ()
    # Every listed company got a SIC (one per run here, all fit).
    assert set(stocks_repo.company_sics(store, as_of=DAY1.date())) == {s.cik for s in first}


@respx.mock
async def test_a_truncated_snapshot_is_refused_and_records_no_departure(
    shipped_settings: ObliskConfig, store_worker: StoreWorker, store: Store
) -> None:
    full = body("sec_company_tickers_exchange.json")
    cut = {**full, "data": full["data"][:3]}
    respx.get(TICKERS).mock(
        side_effect=[httpx.Response(200, json=full), httpx.Response(200, json=cut)]
    )
    respx.get(url__regex=r"https://data\.sec\.gov/submissions/.*").mock(
        return_value=httpx.Response(200, json=body("sec_submissions_spy.json"))
    )
    async with httpx.AsyncClient() as client:
        await listings_collector(shipped_settings, client, store_worker, DAY1).run()
        refused = await listings_collector(shipped_settings, client, store_worker, DAY2).run()
    assert [o.target for o in refused.outcomes if not o.ok] == ["snapshot"]
    assert "refused" in refused.outcomes[0].reason
    assert stocks_repo.left_listings(store, as_of=DAY2.date()) == ()
    assert stocks_repo.snapshot_as_of(store, as_of=DAY2.date()) == stocks_repo.snapshot_as_of(
        store, as_of=DAY1.date()
    )


@respx.mock
async def test_without_a_user_agent_nothing_is_asked(
    shipped_settings: ObliskConfig, store_worker: StoreWorker
) -> None:
    route = respx.get(TICKERS)
    async with httpx.AsyncClient() as client:
        collector = ListingsCollector(
            shipped_settings,
            client=client,
            policy=policy(shipped_settings),
            worker=store_worker,
            user_agent=None,
            sleep=no_sleep,
            now=lambda: DAY1,
        )
        collection = await collector.run()
    assert not route.called
    assert collection.status == "error"


# ── fundamentals ──


def test_the_frames_a_run_reads(shipped_settings: ObliskConfig) -> None:
    config = shipped_settings.sources.stocks.fundamentals
    annual, instant = config.inputs["gross_profit"], config.inputs["assets"]
    assert frame_periods(annual, date(2026, 11, 17), config) == ["CY2026", "CY2025", "CY2024"]
    assert frame_periods(instant, date(2026, 11, 17), config) == ["CY2026Q3I", "CY2026Q2I"]
    assert frame_periods(instant, date(2027, 2, 1), config) == ["CY2026Q4I", "CY2026Q3I"]


@respx.mock
async def test_a_frame_is_kept_as_read_and_a_restatement_is_a_new_row(
    shipped_settings: ObliskConfig, store_worker: StoreWorker, store: Store
) -> None:
    eps = body("sec_frame_eps_cy2025.json")
    restated = json.loads(json.dumps(eps))
    restated["data"][0]["val"] = 19.5
    absent = (FIXTURES / "sec_frame_absent.xml").read_text(encoding="utf-8")

    def answer(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "EarningsPerShareDiluted" in path and path.endswith("CY2025.json"):
            return httpx.Response(200, json=eps if answer.calls == 0 else restated)
        if "GrossProfit" in path and path.endswith("CY2025.json"):
            return httpx.Response(200, json=body("sec_frame_gross_profit_cy2025.json"))
        if "Assets" in path and path.endswith("CY2026Q3I.json"):
            return httpx.Response(200, json=body("sec_frame_assets_cy2026q3i.json"))
        return httpx.Response(404, text=absent)

    answer.calls = 0  # type: ignore[attr-defined]
    respx.get(url__startswith=FRAMES).mock(side_effect=answer)

    async def run(now: datetime) -> Any:
        async with httpx.AsyncClient() as client:
            return await FundamentalsCollector(
                shipped_settings,
                client=client,
                policy=policy(shipped_settings),
                worker=store_worker,
                user_agent="test agent",
                sleep=no_sleep,
                now=lambda: now,
            ).run()

    first = await run(DAY1)
    assert first.status == "ok"  # the frames not built yet are no failure
    assert first.rows == 7 + 3 + 6
    answer.calls = 1  # type: ignore[attr-defined]
    second = await run(DAY2)
    assert second.rows == 1  # JPM's restated value only
    jpm = [f for f in stocks_repo.facts(store, as_of=DAY1.date()) if f.cik == 19617]
    assert [(f.input, f.value) for f in jpm if f.input == "earnings_per_share"] == [
        ("earnings_per_share", 20.02)
    ]
    later = [f for f in stocks_repo.facts(store, as_of=DAY2.date()) if f.cik == 19617]
    assert [f.value for f in later if f.input == "earnings_per_share"] == [19.5]


def test_a_frame_row_of_another_shape_is_refused() -> None:
    with pytest.raises(UpstreamError, match="another shape"):
        parse_frame({"data": [{"cik": 1}]}, input_name="assets", period="CY2026Q3I", known_at=DAY1)


# ── bars ──


@dataclass
class Stamp:
    text: str

    def date(self) -> date:
        return date.fromisoformat(self.text[:10])


class Frame:
    """A recorded yfinance frame read back with the csv module."""

    def __init__(self, rows: list[tuple[str, dict[str, Any]]]) -> None:
        self._rows = rows

    @classmethod
    def from_fixture(cls, name: str) -> Frame:
        with (FIXTURES / name).open(encoding="utf-8") as handle:
            return cls([(row.pop("Date"), dict(row)) for row in csv.DictReader(handle)])

    def iterrows(self) -> Iterable[tuple[Stamp, Mapping[str, Any]]]:
        return [(Stamp(stamp), row) for stamp, row in self._rows]


def test_a_split_is_not_a_move_and_the_running_day_is_not_kept() -> None:
    answer = yahoo_sessions(Frame.from_fixture("yahoo_nvda_split.csv"), "NVDA")
    bars = chain(
        answer, symbol="NVDA", after=date(2024, 6, 3), until=date(2024, 6, 13), known_at=DAY1
    )
    assert next(b.session for b in bars) == date(2024, 6, 4)
    assert bars[-1].session == date(2024, 6, 13)  # the 14th is after `until`
    split = next(b for b in bars if b.session == date(2024, 6, 10))
    assert split.change == pytest.approx(121.4437026977539 / 120.54428100585938 - 1)
    assert split.volume == 313434100
    first_read = chain(answer, symbol="NVDA", after=None, until=date(2024, 6, 14), known_at=DAY1)
    assert first_read[0].change is None
    assert first_read[1].change is not None
    assert yahoo_sessions(Frame.from_fixture("yahoo_twtr_empty.csv"), "TWTR") == ()


def test_the_names_never_read_go_first_then_the_least_recently_read() -> None:
    attempted = {"B": DAY1, "C": DAY1 - timedelta(days=1)}
    assert read_order(["A", "B", "C", "D"], attempted, 3) == ["A", "D", "C"]


async def seed_snapshot(store: Store, tickers: Iterable[str], at: datetime) -> None:
    listings = [
        StockListing(cik=n + 1, ticker=t, name=f"{t} Inc", exchange="NYSE", venue="XNYS")
        for n, t in enumerate(tickers)
    ]
    stocks_repo.save_snapshot(store, listings, known_at=at, content_sha256="0" * 64)


def bars_collector(
    settings: ObliskConfig, worker: StoreWorker, history: Callable[..., Frame], now: datetime
) -> BarsCollector:
    return BarsCollector(
        settings,
        history=history,
        worker=worker,
        pacer=Pacer(settings.collectors, no_sleep),
        sleep=no_sleep,
        now=lambda: now,
    )


async def test_an_empty_answer_is_unresolved_never_a_departure(
    shipped_settings: ObliskConfig, store_worker: StoreWorker, store: Store
) -> None:
    names = ["AAPL", "BBB", "CCC", "DDD", "EEE", "TWTR"]
    await seed_snapshot(store, names, DAY1)

    def history(symbol: str, start: date, end: date) -> Frame:
        return Frame.from_fixture("yahoo_twtr_empty.csv" if symbol == "TWTR" else "yahoo_aapl.csv")

    collection = await bars_collector(fast(shipped_settings), store_worker, history, DAY1).run()
    assert collection.status == "degraded"
    twtr = next(o for o in collection.outcomes if o.target == "TWTR")
    assert twtr.ok
    assert twtr.detail == {"status": "unresolved"}
    assert stocks_repo.left_listings(store, as_of=DAY1.date()) == ()
    attempts = source_repo.target_attempts(store, "stock_bars", "bars:")
    assert set(attempts) == set(names)
    # Stored sessions stop the day before the run (New York) and the first has no change.
    held = stocks_repo.bars_since(store, since=date(2026, 1, 1), as_of=DAY1.date())
    aapl = [b for b in held if b.symbol == "AAPL"]
    assert aapl[-1].session == date(2026, 11, 16)
    assert aapl[0].change is None
    assert all(b.change is not None for b in aapl[1:])


async def test_a_throttled_run_fails_whole_and_records_nothing_about_the_empty(
    shipped_settings: ObliskConfig, store_worker: StoreWorker, store: Store
) -> None:
    await seed_snapshot(store, ["AAPL", "BBB", "CCC"], DAY1)

    def history(symbol: str, start: date, end: date) -> Frame:
        return Frame.from_fixture("yahoo_aapl.csv" if symbol == "AAPL" else "yahoo_twtr_empty.csv")

    collection = await bars_collector(fast(shipped_settings), store_worker, history, DAY1).run()
    assert collection.status == "error"
    assert collection.notes["status"] == "throttled"
    assert collection.judge().reason.startswith("throttled: 2 of 3")
    attempts = source_repo.target_attempts(store, "stock_bars", "bars:")
    assert set(attempts) == {"AAPL"}  # BBB and CCC: nothing recorded, read first next time
    assert stocks_repo.left_listings(store, as_of=DAY1.date()) == ()


async def test_a_read_chains_on_from_the_last_session_held(
    shipped_settings: ObliskConfig, store_worker: StoreWorker, store: Store
) -> None:
    await seed_snapshot(store, ["AAPL"], DAY1)
    asked: list[tuple[date, date]] = []

    def history(symbol: str, start: date, end: date) -> Frame:
        asked.append((start, end))
        return Frame.from_fixture("yahoo_aapl.csv")

    stocks_repo.save_bars(
        store,
        [
            StockBar(
                symbol="AAPL", session=date(2026, 11, 10), close=348.5, volume=1.0, known_at=DAY1
            )
        ],
    )
    await bars_collector(fast(shipped_settings), store_worker, history, DAY2).run()
    assert asked == [(date(2026, 11, 10), date(2026, 11, 17))]
    held = stocks_repo.bars_since(store, since=date(2026, 11, 11), as_of=DAY2.date())
    assert held[0].change == pytest.approx(355.3599853515625 / 348.2453918457031 - 1)
    # A second run the same day has nothing to ask.
    await bars_collector(fast(shipped_settings), store_worker, history, DAY2).run()
    assert len(asked) == 1


# ── the screen ──


def test_months_percentiles_and_a_window_return() -> None:
    assert months_before(date(2026, 3, 31), 1) == date(2026, 2, 28)
    assert months_before(date(2026, 11, 17), 12) == date(2025, 11, 17)
    assert percentile_ranks([3.0, 1.0, 2.0, 2.0]) == [1.0, 0.25, 0.625, 0.625]
    bars = [HeldBar("A", date(2026, 1, d), 1.0, 1.0, 0.1) for d in (2, 5)]
    assert window_return(bars) == pytest.approx(0.21)
    assert window_return([*bars, HeldBar("A", date(2026, 1, 6), 1.0, 1.0, None)]) is None
    assert window_return([]) is None


AS_OF = date(2026, 11, 17)
START = datetime(2025, 10, 1, 22, 0, tzinfo=UTC)
#: Four companies: ticker, cik, SIC, EPS, gross profit, assets, daily change, close, volume.
COMPANIES = (
    ("AAA", 1, 3571, 5.0, 40.0, 100.0, 0.001, 50.0, 1_000_000.0),
    ("BBB", 2, 3571, 1.0, 10.0, 100.0, 0.001, 50.0, 1_000_000.0),
    ("CCC", 3, 6021, 2.0, 30.0, 100.0, -0.001, 50.0, 1_000_000.0),
    ("DDD", 4, 6021, 3.0, None, 100.0, -0.001, 50.0, 1_000_000.0),  # no gross profit
    ("EEE", 5, 3571, 9.0, 90.0, 100.0, 0.001, 50.0, 10.0),  # fails the traded-value floor
)


def weekdays(first: date, last: date) -> list[date]:
    days = [first + timedelta(days=n) for n in range((last - first).days + 1)]
    return [d for d in days if d.weekday() < 5]


def seed_screen(
    store: Store, *, since: date = date(2025, 10, 1), collected: datetime | None = None
) -> None:
    """Bars from ``since``; with ``collected``, everything was first read then (a first read
    loads the bars' history), otherwise each bar was collected on its own day."""
    listed_at = collected or START
    listings = [
        StockListing(cik=cik, ticker=t, name=f"{t} Corp", exchange="NYSE", venue="XNYS")
        for t, cik, *_ in COMPANIES
    ]
    stocks_repo.save_snapshot(store, listings, known_at=listed_at, content_sha256="0" * 64)
    for t, cik, sic, eps, profit, assets, change, close, volume in COMPANIES:
        stocks_repo.save_company(store, cik, sic, known_at=listed_at)
        facts = [("earnings_per_share", "CY2025", eps), ("assets", "CY2026Q3I", assets)]
        if profit is not None:
            facts.append(("gross_profit", "CY2025", profit))
        stocks_repo.save_facts(
            store,
            [
                StockFact(
                    cik=cik,
                    input=name,
                    period=period,
                    period_end=date(2026, 9, 30),
                    value=value,
                    accession="0000000000-26-000001",
                    known_at=listed_at,
                )
                for name, period, value in facts
            ],
        )
        days = weekdays(since, AS_OF - timedelta(days=1))
        stocks_repo.save_bars(
            store,
            [
                StockBar(
                    symbol=t,
                    session=d,
                    close=close,
                    volume=volume,
                    change=None if n == 0 else change,
                    known_at=collected
                    or datetime.combine(d, datetime.min.time(), tzinfo=UTC) + timedelta(hours=23),
                )
                for n, d in enumerate(days)
            ],
        )


def test_the_screen_ranks_the_names_that_pass_the_floors(
    shipped_settings: ObliskConfig, store: Store
) -> None:
    seed_screen(store)
    screen = stock_screen(store, shipped_settings, as_of=AS_OF)
    assert screen.status is ScreenStatus.OK
    assert (screen.listed, screen.passing, screen.lacking) == (5, 4, 1)
    assert [line.ticker for line in screen.lines] == ["AAA", "CCC", "BBB"]
    aaa = screen.lines[0]
    assert aaa.earnings_yield == pytest.approx(0.1)
    assert aaa.gross_profitability == pytest.approx(0.4)
    assert aaa.division == "D"
    assert aaa.industry_trend > 0
    assert aaa.session == date(2026, 11, 16)


def test_bars_held_for_less_than_the_trend_leave_the_screen_immature(
    shipped_settings: ObliskConfig, store: Store
) -> None:
    seed_screen(store, since=date(2026, 6, 1))
    screen = stock_screen(store, shipped_settings, as_of=AS_OF)
    assert screen.status is ScreenStatus.IMMATURE
    assert screen.lines == ()
    assert "12 months" in screen.reason
    assert stock_screen(store, shipped_settings, as_of=date(2025, 9, 1)).status is (
        ScreenStatus.NO_DATA
    )


def screen_header(store: Store, settings: ObliskConfig) -> str:
    from obliskiq.grid.facts import build_fact_sheet

    lines = build_fact_sheet(store, settings, as_of=AS_OF).lines
    return next(line.text for line in lines if line.section == "stock_screen")


def test_a_first_read_s_history_ranks_at_once_and_is_labelled_survivors_only(
    shipped_settings: ObliskConfig, store: Store
) -> None:
    collected = datetime(2026, 11, 10, 22, 0, tzinfo=UTC)
    seed_screen(store, collected=collected)
    screen = stock_screen(store, shipped_settings, as_of=AS_OF)
    assert screen.status is ScreenStatus.OK
    assert [line.ticker for line in screen.lines] == ["AAA", "CCC", "BBB"]
    assert screen.collected_from == date(2026, 11, 10)
    assert screen.survivors_only_until == date(2027, 11, 10)
    # Before the first read nothing was known, whatever sessions the bars carry.
    assert stock_screen(store, shipped_settings, as_of=date(2026, 11, 9)).status is (
        ScreenStatus.NO_DATA
    )
    header = screen_header(store, shipped_settings)
    assert "until 2027-11-10" in header
    assert "still listed on 2026-11-10" in header
    assert "survivors only" in header


def test_a_window_inside_the_collection_carries_no_survivors_label(
    shipped_settings: ObliskConfig, store: Store
) -> None:
    seed_screen(store)
    screen = stock_screen(store, shipped_settings, as_of=AS_OF)
    assert (screen.collected_from, screen.survivors_only_until) == (None, None)
    assert "survivors only" not in screen_header(store, shipped_settings)


def test_what_was_collected_after_the_date_changes_nothing(
    shipped_settings: ObliskConfig, store: Store
) -> None:
    from obliskiq.grid.facts import build_fact_sheet

    seed_screen(store)
    before = stock_screen(store, shipped_settings, as_of=AS_OF)
    sheet = build_fact_sheet(store, shipped_settings, as_of=AS_OF).text()
    later = datetime(2026, 11, 18, 21, 0, tzinfo=UTC)
    # A new snapshot without AAA, a restated EPS, a new company and a bar, all known later.
    stocks_repo.save_snapshot(
        store,
        [StockListing(cik=2, ticker="BBB", name="BBB Corp", exchange="NYSE", venue="XNYS")],
        known_at=later,
        content_sha256="1" * 64,
    )
    stocks_repo.save_facts(
        store,
        [
            StockFact(
                cik=2,
                input="earnings_per_share",
                period="CY2025",
                period_end=date(2026, 9, 30),
                value=50.0,
                accession="0000000000-26-000002",
                known_at=later,
            )
        ],
    )
    stocks_repo.save_company(store, 99, 3571, known_at=later)
    stocks_repo.save_bars(
        store,
        [
            StockBar(
                symbol="BBB",
                session=date(2026, 11, 17),
                close=1.0,
                volume=1.0,
                change=0.5,
                known_at=later,
            )
        ],
    )
    assert stock_screen(store, shipped_settings, as_of=AS_OF) == before
    assert build_fact_sheet(store, shipped_settings, as_of=AS_OF).text() == sheet


def test_the_sheet_carries_the_screen_labelled_untested(
    shipped_settings: ObliskConfig, store: Store
) -> None:
    from obliskiq.grid.facts import build_fact_sheet

    seed_screen(store)
    lines = [
        line
        for line in build_fact_sheet(store, shipped_settings, as_of=AS_OF).lines
        if line.section == "stock_screen"
    ]
    assert "no signal" in lines[0].text
    named = [line for line in lines[1:] if line.text.startswith("#")]
    assert [line.text.split()[1] for line in named] == ["AAA", "CCC", "BBB"]
    assert all(line.text.endswith("untested (D180)") for line in named)
    assert all(line.day == date(2026, 11, 16) for line in named)
    assert lines[-1].status == "n/a"
    assert lines[-1].text.startswith("1 of the 4")


def test_an_immature_or_failed_screen_is_a_status_line_and_no_name(
    shipped_settings: ObliskConfig, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    from obliskiq.grid import facts

    seed_screen(store, since=date(2026, 6, 1))

    def screen_lines() -> list[Any]:
        sheet = facts.build_fact_sheet(store, shipped_settings, as_of=AS_OF)
        return [line for line in sheet.lines if line.section == "stock_screen"]

    (immature,) = screen_lines()
    assert immature.status == "immature"
    assert "untested" not in immature.text.split("; ")[-1]

    def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("store unreadable")

    monkeypatch.setattr(facts, "stock_screen", broken)
    (failed,) = screen_lines()
    assert failed.status == "failed"
    assert "cannot be built: RuntimeError" in failed.text


def test_the_sheet_with_and_without_the_screen_is_another_segment(
    shipped_settings: ObliskConfig,
) -> None:
    from obliskiq.grid.facts import SECTIONS
    from obliskiq.llm.analyst_prompts import analyst_prompt_hash

    without = tuple(section for section in SECTIONS if section != "stock_screen")
    assert "stock_screen" in SECTIONS
    assert analyst_prompt_hash(shipped_settings) == analyst_prompt_hash(
        shipped_settings, sections=SECTIONS
    )
    assert analyst_prompt_hash(shipped_settings, sections=without) != analyst_prompt_hash(
        shipped_settings
    )


def test_the_digest_line_counts_the_listed_and_the_departed(
    shipped_settings: ObliskConfig, store: Store
) -> None:
    assert "no_data" in screen_summary(store, shipped_settings, as_of=AS_OF)
    seed_screen(store)
    stocks_repo.save_snapshot(
        store,
        [StockListing(cik=2, ticker="BBB", name="BBB Corp", exchange="NYSE", venue="XNYS")],
        known_at=datetime(2026, 11, 16, 22, 0, tzinfo=UTC),
        content_sha256="1" * 64,
    )
    line = screen_summary(store, shipped_settings, as_of=AS_OF)
    assert line.startswith("stock screen (untested, D180): 1 names listed, 4 left since 2025-10-01")


# ── failure branches ──


def test_answers_of_another_shape_are_refused(shipped_settings: ObliskConfig) -> None:
    config = shipped_settings.sources.stocks.listings
    with pytest.raises(UpstreamError, match="not \\{fields, data\\}"):
        parse_listings([], config)
    fields = ["cik", "name", "ticker", "exchange"]
    with pytest.raises(UpstreamError, match="another shape"):
        parse_listings({"fields": fields, "data": [[1, "x"]]}, config)
    with pytest.raises(UpstreamError, match="not a number"):
        parse_sic({"sic": "12a"})
    with pytest.raises(UpstreamError, match="is not"):
        parse_frame([], input_name="assets", period="CY2026Q3I", known_at=DAY1)
    with pytest.raises(StoreError, match="at least one"):
        stocks_repo.save_snapshot(None, [], known_at=DAY1, content_sha256="0" * 64)  # type: ignore[arg-type]


@respx.mock
async def test_a_dry_run_keeps_nothing_and_a_file_without_stocks_or_a_company_fails(
    shipped_settings: ObliskConfig, store_worker: StoreWorker, store: Store
) -> None:
    full = body("sec_company_tickers_exchange.json")
    otc_only = {**full, "data": [row for row in full["data"] if row[3] == "OTC"]}
    respx.get(TICKERS).mock(
        side_effect=[
            httpx.Response(200, json=full),
            httpx.Response(200, json=otc_only),
            httpx.Response(200, json=full),
        ]
    )
    respx.get(url__regex=r"https://data\.sec\.gov/submissions/.*").mock(
        return_value=httpx.Response(503, text="down")
    )
    async with httpx.AsyncClient() as client:
        dry = await listings_collector(shipped_settings, client, store_worker, DAY1).run(
            dry_run=True
        )
        empty = await listings_collector(shipped_settings, client, store_worker, DAY1).run()
        kept = await listings_collector(shipped_settings, client, store_worker, DAY2).run()
    assert dry.status == "ok"
    assert stocks_repo.latest_snapshot_held(store) is not None
    assert "no common stock" in empty.outcomes[0].reason
    sic = next(o for o in kept.outcomes if o.target == "sic")
    assert sic.degraded
    assert "HTTP 503" in sic.reason
    assert stocks_repo.company_sics(store, as_of=DAY2.date()) == {}


async def test_a_broken_answer_fails_its_name_and_a_dry_run_records_nothing(
    shipped_settings: ObliskConfig, store_worker: StoreWorker, store: Store
) -> None:
    await seed_snapshot(store, ["AAPL", "BBB", "CCC", "DDD", "EEE", "TWTR", "OLD"], DAY1)
    stocks_repo.save_bars(
        store,
        [StockBar(symbol="OLD", session=date(2026, 11, 16), close=1.0, volume=1.0, known_at=DAY1)],
    )

    def history(symbol: str, start: date, end: date) -> Frame:
        if symbol == "BBB":
            raise RuntimeError("connection reset")
        return Frame.from_fixture("yahoo_twtr_empty.csv" if symbol == "TWTR" else "yahoo_aapl.csv")

    settings = fast(shipped_settings)
    dry = await bars_collector(settings, store_worker, history, DAY1).run(dry_run=True)
    assert source_repo.target_attempts(store, "stock_bars", "bars:") == {}
    assert next(o for o in dry.outcomes if o.target == "OLD").reason == "up to date"
    run = await bars_collector(settings, store_worker, history, DAY1).run()
    assert run.status == "partial"
    assert "RuntimeError" in next(o for o in run.outcomes if o.target == "BBB").reason
    assert set(source_repo.target_attempts(store, "stock_bars", "bars:")) == {
        "AAPL",
        "BBB",
        "CCC",
        "DDD",
        "EEE",
        "TWTR",
        "OLD",
    }


def test_the_screen_s_floors_and_inputs_leave_out_what_they_should(
    shipped_settings: ObliskConfig, store: Store
) -> None:
    seed_screen(store)
    later = START + timedelta(hours=1)
    # A second listing of company 1, less traded: one line for the company.
    # A name too dear for one share at the position, a name with a stale close, a name whose
    # facts are too old, and a company with no SIC.
    extra = [
        ("AAB", 1, 50.0, 10_000.0, AS_OF - timedelta(days=1)),
        ("DEAR", 6, 5_000.0, 1_000_000.0, AS_OF - timedelta(days=1)),
        ("STALE", 7, 50.0, 1_000_000.0, AS_OF - timedelta(days=30)),
        ("OLDF", 8, 50.0, 1_000_000.0, AS_OF - timedelta(days=1)),
        ("NOSIC", 9, 50.0, 1_000_000.0, AS_OF - timedelta(days=1)),
    ]
    stocks_repo.save_snapshot(
        store,
        [
            *stocks_repo.listed(store, stocks_repo.snapshot_as_of(store, as_of=AS_OF)),  # type: ignore[arg-type]
            *(
                StockListing(cik=cik, ticker=t, name=f"{t} Corp", exchange="NYSE", venue="XNYS")
                for t, cik, *_ in extra
            ),
        ],
        known_at=later,
        content_sha256="2" * 64,
    )
    for t, cik, close, volume, last in extra:
        if cik != 9:
            stocks_repo.save_company(store, cik, 3571, known_at=later)
        else:
            stocks_repo.save_company(store, 9, None, known_at=later)
        end = date(2020, 1, 31) if cik == 8 else date(2026, 9, 30)
        stocks_repo.save_facts(
            store,
            [
                StockFact(
                    cik=cik,
                    input=name,
                    period="CY2025",
                    period_end=end,
                    value=1.0,
                    accession="0000000000-26-000003",
                    known_at=later,
                )
                for name in ("earnings_per_share", "gross_profit", "assets")
            ],
        )
        days = weekdays(last - timedelta(days=40), last)
        stocks_repo.save_bars(
            store,
            [
                StockBar(
                    symbol=t, session=d, close=close, volume=volume, change=None, known_at=later
                )
                for d in days
            ],
        )
    screen = stock_screen(store, shipped_settings, as_of=AS_OF)
    tickers = [line.ticker for line in screen.lines]
    assert "AAB" not in tickers
    assert "AAA" in tickers
    assert not {"DEAR", "STALE"} & {line.ticker for line in screen.lines}
    assert not {"OLDF", "NOSIC"} & set(tickers)
    assert screen.passing == 6  # AAA, BBB, CCC, DDD, OLDF, NOSIC
    assert screen.lacking == 3


def test_a_screen_with_no_complete_name_ranks_none(
    shipped_settings: ObliskConfig, store: Store
) -> None:
    seed_screen(store)
    screen = stock_screen(store, shipped_settings, as_of=AS_OF)
    stocks = shipped_settings.sources.stocks
    fundamentals = stocks.fundamentals.model_copy(update={"max_age_days": 1})
    settings = shipped_settings.model_copy(
        update={
            "sources": shipped_settings.sources.model_copy(
                update={"stocks": stocks.model_copy(update={"fundamentals": fundamentals})}
            )
        }
    )
    empty = stock_screen(store, settings, as_of=AS_OF)
    assert screen.lines
    assert empty.status is ScreenStatus.OK
    assert empty.lines == ()
    assert empty.lacking == empty.passing
    assert stocks_repo.save_bars(store, []) == 0
