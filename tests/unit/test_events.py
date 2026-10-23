"""Events: prediction markets (Gamma and Kalshi parsed, collected, read with velocity), GDELT's
complete days and Welford pulse, the news feeds (RSS 2.0, RDF, Atom; refusals; dedupe) with their
fallback tone, and research-grade earnings dates.

Ported in intent from the old ObliskIQ's ``tests/test_polymarket_client.py`` (Yes price from the
encoded lists, velocity against the prior snapshot, an unreachable market dropped) and
``tests/test_gdelt_client.py`` (Welford z against the distribution before the observation, warm-up
degradation, a throttled feed not hammered for every region).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import json
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from obliskiq.config import ObliskConfig
from obliskiq.config.loader import load_config
from obliskiq.config.schema_sources import FeedConfig
from obliskiq.data.events.earnings import EarningsCollector, select_dates
from obliskiq.data.events.gdelt import GdeltCollector, HourPoint, complete_days, hourly
from obliskiq.data.events.markets import (
    PredictionMarkets,
    kalshi_page,
    kalshi_probability,
    polymarket_page,
    yes_probability,
)
from obliskiq.data.events.readings import Welford, event_reading, pulse_reading
from obliskiq.data.events.rss import (
    NewsCollector,
    filing_text,
    item_id,
    parse_feed,
    plain,
    submission_url,
)
from obliskiq.data.events.tone import FallbackTone
from obliskiq.data.market.plumbing import Pacer
from obliskiq.domain.sources import EventSnapshot, GdeltDay
from obliskiq.errors import UpstreamError
from obliskiq.store import Store
from obliskiq.store import sources as source_repo
from obliskiq.store.worker import StoreWorker
from obliskiq.util.http import RetryPolicy

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "events"
NOW = datetime(2026, 10, 23, 21, 15, tzinfo=UTC)
GAMMA = "https://gamma-api.polymarket.com/events"
KALSHI = "https://api.elections.kalshi.com/trade-api/v2/markets"
GDELT = "https://api.gdeltproject.org/api/v2/doc/doc"


#: The shipped sections with a 24-hour velocity window and a 20-day warm-up.
SHIPPED = load_config(Path(__file__).resolve().parents[2] / "config").settings.sources.events
EVENTS = SHIPPED.model_copy(update={"velocity_hours": 24})
GDELT_20 = SHIPPED.gdelt.model_copy(update={"min_history_days": 20})


def fixture_json(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


async def no_sleep(_seconds: float) -> None:
    return None


def fast_pacer(settings: ObliskConfig) -> Pacer:
    retry = settings.collectors.retry.model_copy(
        update={"target_retries": 0, "request_spacing_seconds": 0.0}
    )
    return Pacer(settings.collectors.model_copy(update={"retry": retry}), no_sleep)


def policy(settings: ObliskConfig) -> RetryPolicy:
    return RetryPolicy.from_settings(settings.http.model_copy(update={"retries": 0}))


def with_sources(settings: ObliskConfig, **sections: Any) -> ObliskConfig:
    sources = settings.sources.model_copy(update=sections)
    return settings.model_copy(update={"sources": sources})


# ── prediction markets: parsing ──


def test_the_yes_price_is_read_from_gamma_s_encoded_lists() -> None:
    assert yes_probability({"outcomes": '["Yes","No"]', "outcomePrices": '["0.82","0.18"]'}) == (
        pytest.approx(0.82)
    )
    assert yes_probability({"outcomes": ["No", "Yes"], "outcomePrices": ["0.3", "0.7"]}) == 0.7


@pytest.mark.parametrize(
    "market",
    [
        {"outcomes": '["Up","Down"]', "outcomePrices": '["0.6","0.4"]'},  # no Yes: no stand-in
        {"outcomes": '["Yes","No"]', "outcomePrices": '["0.6"]'},
        {"outcomes": "not json", "outcomePrices": '["0.6","0.4"]'},
        {"outcomes": '["Yes","No"]', "outcomePrices": '["1.6","0.4"]'},
        {"outcomes": None, "outcomePrices": None, "lastTradePrice": 0.5},
    ],
)
def test_a_market_without_a_usable_yes_price_has_none(market: dict[str, Any]) -> None:
    assert yes_probability(market) is None


def test_kalshi_reads_the_midpoint_of_a_tight_quote_else_the_last_trade() -> None:
    markets = fixture_json("kalshi_kxfeddecision.json")["markets"]
    readings = [kalshi_probability(m, max_mid_spread=0.10) for m in markets]
    # 0.08/0.21 is wider than ten cents (last 0.22); 0.13/0.23 and 0.54/0.64 are exactly ten.
    assert readings == [pytest.approx(0.22), pytest.approx(0.18), pytest.approx(0.59)]


@pytest.mark.parametrize(
    ("market", "expected"),
    [
        ({"yes_bid_dollars": "0.3", "yes_ask_dollars": "0.9", "last_price_dollars": "0"}, None),
        ({"yes_bid_dollars": "0.5", "yes_ask_dollars": "0.4", "last_price_dollars": "0.45"}, 0.45),
        ({"yes_bid_dollars": "x", "yes_ask_dollars": "0.4", "last_price_dollars": "nan"}, None),
        ({"yes_bid_dollars": "0.2", "yes_ask_dollars": "1.5", "last_price_dollars": None}, None),
    ],
)
def test_kalshi_without_a_tight_quote_or_a_trade_has_no_probability(
    market: dict[str, Any], expected: float | None
) -> None:
    assert kalshi_probability(market, max_mid_spread=0.10) == expected


def test_a_gamma_event_becomes_markets_and_snapshots() -> None:
    page = polymarket_page(
        fixture_json("polymarket_event_fed_december.json"),
        curated=True,
        observed_at=NOW,
        min_liquidity=None,
    )
    assert page.left_out == 0
    assert len(page.readings) == 3
    market, snapshot = page.readings[2].market, page.readings[2].snapshot
    assert market.market_id == "3215007"
    assert market.event_id == "770450"
    assert market.outcome == "Yes"
    assert market.curated
    assert market.tags == ("economy", "fed", "fed-rates", "fomc", "trump")
    assert market.closes_at == datetime(2026, 12, 9, 23, 59, tzinfo=UTC)
    assert (snapshot.probability, snapshot.bid, snapshot.ask) == (0.225, 0.22, 0.23)
    assert snapshot.observed_at == NOW
    assert snapshot.liquidity == pytest.approx(161035.4135)


def test_closed_inactive_illiquid_and_unpriced_markets_are_left_out() -> None:
    event = fixture_json("polymarket_event_fed_december.json")[0]
    first, second, third = event["markets"]
    event["markets"] = [
        {**first, "closed": True},
        {**second, "active": False},
        {**third, "outcomes": '["Up","Down"]'},
        "not a market",
    ]
    page = polymarket_page([event], curated=True, observed_at=NOW, min_liquidity=None)
    assert page.readings == ()
    assert page.left_out == 1
    discovered = polymarket_page(
        fixture_json("polymarket_tag_recession.json"),
        curated=False,
        observed_at=NOW,
        min_liquidity=10_000,
    )
    assert discovered.readings == ()
    assert discovered.left_out == 0


@pytest.mark.parametrize("body", [{"events": []}, ["not an event"]])
def test_a_gamma_answer_of_another_shape_is_an_error(body: Any) -> None:
    with pytest.raises(UpstreamError):
        polymarket_page(body, curated=True, observed_at=NOW, min_liquidity=None)


def test_a_kalshi_page_keeps_its_cursor_and_counts_what_it_left_out() -> None:
    body = fixture_json("kalshi_kxfeddecision.json")
    body["markets"].append({"ticker": "KX-NOQUOTE", "title": "Nobody quotes it"})
    page = kalshi_page(body, series="KXFEDDECISION", observed_at=NOW, max_mid_spread=0.10)
    assert [r.market.market_id for r in page.readings] == [
        "KXFEDDECISION-28JAN-H26",
        "KXFEDDECISION-28JAN-H25",
        "KXFEDDECISION-28JAN-H0",
    ]
    assert page.left_out == 1
    assert page.cursor == body["cursor"]
    market, snapshot = page.readings[0].market, page.readings[0].snapshot
    assert market.tags == ("KXFEDDECISION",)
    assert market.event_id == "KXFEDDECISION-28JAN"
    assert snapshot.volume == pytest.approx(2458.31)
    assert snapshot.liquidity is None
    for wrong in ({"cursor": ""}, {"markets": ["x"]}, []):
        with pytest.raises(UpstreamError):
            kalshi_page(wrong, series="S", observed_at=NOW, max_mid_spread=0.10)


# ── prediction markets: collection ──


def markets_collector(
    settings: ObliskConfig, worker: StoreWorker, client: httpx.AsyncClient
) -> PredictionMarkets:
    return PredictionMarkets(
        settings,
        client=client,
        policy=policy(settings),
        worker=worker,
        pacer=fast_pacer(settings),
        now=lambda: NOW,
    )


async def test_polymarket_reads_curated_events_then_discovers_by_tag(
    shipped_settings: ObliskConfig, store_worker: StoreWorker, store: Store
) -> None:
    december = fixture_json("polymarket_event_fed_december.json")
    discovered = json.loads(json.dumps(december))
    discovered[0]["id"] = "999"
    for market in discovered[0]["markets"]:
        market["id"] = f"9{market['id']}"

    def answer(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        if params.get("slug") == "fed-decision-in-december-20260729232808632":
            return httpx.Response(200, json=december)
        if params.get("slug") == "october-inflation-us-monthly":
            return httpx.Response(500, text="down")
        if params.get("slug") == "how-many-fed-rate-cuts-in-2026":
            return httpx.Response(200, text="<html>")
        if params.get("tag_slug") == "tariffs":
            return httpx.Response(502, text="bad gateway")
        if params.get("tag_slug") == "fed-rates":
            assert params["closed"] == "false"
            assert params["limit"] == "20"
            return httpx.Response(200, json=december + discovered)
        return httpx.Response(200, json=[])

    async with httpx.AsyncClient() as client:
        with respx.mock as router:
            router.get(GAMMA).mock(side_effect=answer)
            result = await markets_collector(shipped_settings, store_worker, client).polymarket()
    outcomes = {o.target: o for o in result.outcomes}
    assert outcomes["event:fed-decision-in-december-20260729232808632"].rows == 3
    assert not outcomes["event:october-inflation-us-monthly"].ok
    assert "not JSON" in outcomes["event:how-many-fed-rate-cuts-in-2026"].reason
    ended = outcomes["event:us-recession-by-end-of-2026"]
    assert ended.ok
    assert ended.degraded
    assert "event ended" in ended.reason
    assert outcomes["tag:fed-rates"].rows == 6
    assert not outcomes["tag:recession"].degraded
    assert "HTTP 502" in outcomes["tag:tariffs"].reason
    assert result.status == "partial"
    markets = {m.market_id: m for m in source_repo.event_markets(store, as_of=NOW.date())}
    assert markets["3215005"].curated  # also found by tag: a curated market stays curated
    assert not markets["93215005"].curated
    assert len(markets) == 6
    assert source_repo.event_markets(store, as_of=NOW.date() - timedelta(days=1)) == ()
    held = source_repo.event_snapshots(store, "polymarket", "3215007", as_of=NOW.date())
    assert [s.probability for s in held] == [0.225]


async def test_kalshi_pages_each_series_up_to_its_cap_and_a_dry_run_writes_nothing(
    shipped_settings: ObliskConfig, store_worker: StoreWorker, store: Store
) -> None:
    body = fixture_json("kalshi_kxfeddecision.json")
    first = {"cursor": "next", "markets": body["markets"][:2]}
    second = {"cursor": "", "markets": body["markets"][2:]}
    seen: list[dict[str, str]] = []

    def answer(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        seen.append(params)
        if params["series_ticker"] == "KXFED":
            return httpx.Response(503, text="busy")
        if params["series_ticker"] == "KXRECSSNBER":
            return httpx.Response(200, json={"markets": [], "cursor": ""})
        return httpx.Response(200, json=second if params.get("cursor") == "next" else first)

    async with httpx.AsyncClient() as client:
        with respx.mock as router:
            router.get(KALSHI).mock(side_effect=answer)
            dry = await markets_collector(shipped_settings, store_worker, client).kalshi(
                dry_run=True
            )
            capped = with_sources(
                shipped_settings,
                events=shipped_settings.sources.events.model_copy(
                    update={
                        "kalshi": shipped_settings.sources.events.kalshi.model_copy(
                            update={"max_markets_per_series": 2}
                        )
                    }
                ),
            )
            short = await markets_collector(capped, store_worker, client).kalshi()
    outcomes = {o.target: o for o in dry.outcomes}
    assert outcomes["series:KXFEDDECISION"].rows == 3
    assert [p.get("limit") for p in seen[:2]] == ["60", "58"]
    assert not outcomes["series:KXFED"].ok
    assert outcomes["series:KXRECSSNBER"].degraded
    assert {o.target: o.rows for o in short.outcomes}["series:KXFEDDECISION"] == 2
    held = source_repo.event_markets(store, as_of=NOW.date())
    assert [m.market_id for m in held] == ["KXFEDDECISION-28JAN-H25", "KXFEDDECISION-28JAN-H26"]


# ── prediction markets: readings ──


def snapshot(hours_before: float, probability: float) -> EventSnapshot:
    return EventSnapshot(
        venue="polymarket",
        market_id="m",
        observed_at=NOW - timedelta(hours=hours_before),
        probability=probability,
        bid=None,
        ask=None,
        last_price=None,
        volume=None,
        liquidity=None,
    )


def test_velocity_is_the_change_against_the_newest_snapshot_a_window_older() -> None:
    held = [snapshot(48, 0.40), snapshot(30, 0.55), snapshot(6, 0.62), snapshot(0, 0.70)]
    reading = event_reading(held, at=NOW, config=EVENTS)
    assert reading is not None
    assert reading.probability == 0.70
    assert reading.velocity == pytest.approx(0.15)
    assert reading.reference_at == NOW - timedelta(hours=30)
    earlier = event_reading(held, at=NOW - timedelta(hours=1), config=EVENTS)
    assert earlier is not None
    assert earlier.probability == 0.62
    assert earlier.velocity == pytest.approx(0.07)


def test_without_an_older_snapshot_velocity_is_none_and_without_any_there_is_no_reading() -> None:
    reading = event_reading([snapshot(2, 0.3), snapshot(0, 0.4)], at=NOW, config=EVENTS)
    assert reading is not None
    assert reading.velocity is None
    assert reading.reference_at is None
    assert event_reading([], at=NOW, config=EVENTS) is None
    assert event_reading([snapshot(0, 0.4)], at=NOW - timedelta(hours=1), config=EVENTS) is None


# ── GDELT ──


def test_a_timeline_is_read_hour_by_hour() -> None:
    points = hourly(fixture_json("gdelt_middle_east_volraw.json"))
    assert len(points) == 162
    assert points[0] == HourPoint(datetime(2026, 10, 16, 7, tzinfo=UTC), 603, 12786)
    assert hourly({}) == []
    assert hourly({"timeline": []}) == []


@pytest.mark.parametrize(
    "body",
    [
        [],
        {"timeline": "x"},
        {"timeline": [{"series": "s"}]},
        {"timeline": [{"data": ["x"]}]},
        {"timeline": [{"data": [{"date": "yesterday", "value": 1}]}]},
        {"timeline": [{"data": [{"date": "20261016T070000Z", "value": None}]}]},
    ],
)
def test_a_timeline_of_another_shape_is_an_error(body: Any) -> None:
    with pytest.raises(UpstreamError):
        hourly(body)


def test_only_whole_days_before_today_are_kept_with_an_article_weighted_tone() -> None:
    volume = hourly(fixture_json("gdelt_middle_east_volraw.json"))
    tone = hourly(fixture_json("gdelt_latam_tone.json"))
    days = complete_days(
        volume, tone, query_id="middle_east", today=date(2026, 10, 23), collected_at=NOW
    )
    # 16 October starts mid-day and 23 October is today; 18 October lacks three hours but is
    # whole: the hours GDELT left out count in neither total.
    assert [d.day for d in days] == [date(2026, 10, d) for d in range(17, 23)]
    assert (days[1].articles, days[1].monitored) == (4165, 76163)
    assert days[1].share == pytest.approx(4165 / 76163)
    assert all(d.tone is not None and -10 < d.tone < 10 for d in days)
    hour = datetime(2026, 10, 18, 12, tzinfo=UTC)
    two = [
        HourPoint(hour - timedelta(days=1), 1, 10),
        HourPoint(hour, 30, 100),
        HourPoint(hour + timedelta(hours=1), 10, 100),
        HourPoint(hour + timedelta(hours=2), 0, 100),
    ]
    tones = [HourPoint(hour, -2.0, None), HourPoint(hour + timedelta(hours=2), 9.0, None)]
    (weighted,) = complete_days(
        two, tones, query_id="q", today=date(2026, 10, 19), collected_at=NOW
    )
    assert weighted.tone == -2.0
    assert weighted.articles == 40
    assert complete_days([], [], query_id="q", today=date(2026, 10, 19), collected_at=NOW) == []
    blank = [HourPoint(hour - timedelta(days=1), 1, 10), HourPoint(hour, 0, None)]
    assert complete_days(blank, [], query_id="q", today=date(2026, 10, 19), collected_at=NOW) == []
    (toneless,) = complete_days(
        two[:2], [], query_id="q", today=date(2026, 10, 19), collected_at=NOW
    )
    assert toneless.tone is None


class Sleeps:
    def __init__(self) -> None:
        self.waits: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


def gdelt_collector(
    settings: ObliskConfig, worker: StoreWorker, client: httpx.AsyncClient, sleep: Sleeps
) -> GdeltCollector:
    return GdeltCollector(
        settings,
        client=client,
        policy=policy(settings),
        worker=worker,
        now=lambda: NOW,
        sleep=sleep,
    )


async def test_gdelt_stores_new_whole_days_once_and_waits_out_a_429(
    shipped_settings: ObliskConfig, store_worker: StoreWorker, store: Store
) -> None:
    volume = fixture_json("gdelt_middle_east_volraw.json")
    tone = fixture_json("gdelt_latam_tone.json")
    throttled = {"once": True}

    def answer(request: httpx.Request) -> httpx.Response:
        if throttled.pop("once", False):
            return httpx.Response(429, text="Please limit requests to one every 5 seconds")
        return httpx.Response(
            200, json=volume if request.url.params["mode"] == "timelinevolraw" else tone
        )

    sleeps = Sleeps()
    async with httpx.AsyncClient() as client:
        with respx.mock as router:
            router.get(GDELT).mock(side_effect=answer)
            first = await gdelt_collector(shipped_settings, store_worker, client, sleeps).run()
            again = await gdelt_collector(shipped_settings, store_worker, client, Sleeps()).run()
            dry = await gdelt_collector(shipped_settings, store_worker, client, Sleeps()).run(
                dry_run=True
            )
    gdelt = shipped_settings.sources.events.gdelt
    assert first.status == "ok"
    assert first.rows == 6 * len(gdelt.queries)
    assert sleeps.waits[:3] == [12, 60, 12]  # spaced, backed off after the 429, spaced again
    assert again.rows == 0
    assert dry.rows == 0
    held = source_repo.gdelt_days(store, "middle_east", as_of=NOW.date())
    assert len(held) == 6
    assert source_repo.gdelt_days(store, "middle_east", as_of=date(2026, 10, 22)) == ()
    assert {o.detail["theme"] for o in first.outcomes} == {"conflict", "politics"}


async def test_a_throttled_gdelt_is_not_asked_for_every_query(
    shipped_settings: ObliskConfig, store_worker: StoreWorker
) -> None:
    calls = []

    def answer(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.params["query"])
        return httpx.Response(429, text="Please limit requests")

    async with httpx.AsyncClient() as client:
        with respx.mock as router:
            router.get(GDELT).mock(side_effect=answer)
            result = await gdelt_collector(shipped_settings, store_worker, client, Sleeps()).run()
    gdelt = shipped_settings.sources.events.gdelt
    asked = gdelt.give_up_after_failed_queries * (gdelt.rate_limited_retries + 1)
    assert len(calls) == asked  # the volume timeline of two queries, each asked three times
    reasons = [o.reason for o in result.outcomes]
    assert all("429" in r for r in reasons[:2])
    assert all("not asked" in r for r in reasons[2:])
    assert result.status == "error"


async def test_one_good_gdelt_query_resets_the_count_and_errors_are_named(
    shipped_settings: ObliskConfig, store_worker: StoreWorker
) -> None:
    volume = fixture_json("gdelt_middle_east_volraw.json")
    queries = list(shipped_settings.sources.events.gdelt.queries.values())

    def answer(request: httpx.Request) -> httpx.Response:
        query = request.url.params["query"]
        if query == queries[0].query:
            return httpx.Response(500, text="down")
        if query == queries[1].query:
            return httpx.Response(200, json={})
        if query == queries[2].query:
            return httpx.Response(200, text="<html>")
        return httpx.Response(200, json=volume)

    async with httpx.AsyncClient() as client:
        with respx.mock as router:
            router.get(GDELT).mock(side_effect=answer)
            result = await gdelt_collector(shipped_settings, store_worker, client, Sleeps()).run(
                dry_run=True
            )
    outcomes = [(o.target, o.ok, o.degraded, o.reason) for o in result.outcomes]
    assert "HTTP 500" in outcomes[0][3]
    assert outcomes[1][1:] == (True, True, "no complete day in the span")  # nothing matched
    assert "not JSON" in outcomes[2][3]  # asked: the empty answer reset the count
    assert outcomes[3][1:] == (True, False, "")


def day(n: int, share: float, tone: float | None = -1.0, *, collected: int = 0) -> GdeltDay:
    return GdeltDay(
        query_id="q",
        day=date(2026, 1, 1) + timedelta(days=n),
        articles=round(share * 10_000),
        monitored=10_000,
        tone=tone,
        collected_at=datetime(2026, 1, 2, tzinfo=UTC) + timedelta(days=n + collected),
    )


def test_welford_scores_against_the_distribution_before_the_observation() -> None:
    mature = Welford(count=20, mean=100.0, m2=100.0 * 19)  # standard deviation 10
    assert mature.z(150.0) == pytest.approx(5.0)
    assert Welford().add(100.0).z(150.0) is None
    stats = Welford.of([1.0, 2.0, 4.0, 7.0])
    assert stats.mean == pytest.approx(3.5)
    assert stats.m2 / 3 == pytest.approx(7.0)
    assert Welford.of([2.0, 2.0, 2.0]).z(3.0) is None


def test_the_pulse_warms_up_then_scores_the_newest_day_point_in_time() -> None:
    days = [day(n, 0.10 + 0.01 * (n % 3), -1.0 - 0.1 * (n % 2)) for n in range(21)]
    days.append(day(21, 0.30, -6.0))
    reading = pulse_reading(days, query_id="q", as_of=date(2026, 1, 23), config=GDELT_20)
    assert not reading.warming_up
    assert reading.history == 21
    assert reading.day == date(2026, 1, 22)
    assert reading.share_z is not None
    assert reading.share_z > 10
    assert reading.tone_z is not None
    assert reading.tone_z < -10
    before = pulse_reading(days, query_id="q", as_of=date(2026, 1, 21), config=GDELT_20)
    assert before.warming_up
    assert before.share_z is None
    assert before.history == 19
    empty = pulse_reading(days, query_id="other", as_of=date(2026, 1, 23), config=GDELT_20)
    assert empty.day is None
    assert empty.warming_up
    assert empty.history == 0


def test_a_day_without_tone_scores_its_share_only() -> None:
    days = [
        day(n, 0.10 + 0.01 * (n % 3), None if n < 5 else -1.0 - 0.1 * (n % 2)) for n in range(21)
    ]
    days.append(day(21, 0.30, None))
    toneless = pulse_reading(days, query_id="q", as_of=date(2026, 1, 23), config=GDELT_20)
    assert toneless.share_z is not None
    assert toneless.tone_z is None
    days[-1] = day(21, 0.30, -6.0)
    thin = pulse_reading(days, query_id="q", as_of=date(2026, 1, 23), config=GDELT_20)
    assert thin.tone_z is None  # only 16 earlier days carry a tone


# ── news feeds ──


def tone(settings: ObliskConfig) -> FallbackTone:
    return FallbackTone(settings.sources.news.tone_lexicon)


def feed(settings: ObliskConfig, name: str) -> FeedConfig:
    return settings.sources.news.feeds[name]


def test_rss_2_rdf_and_atom_feeds_are_read(shipped_settings: ObliskConfig) -> None:
    scorer = tone(shipped_settings)
    fed = parse_feed(
        (FIXTURES / "rss_fed_press.xml").read_bytes(),
        feed_id="fed_press",
        feed=feed(shipped_settings, "fed_press"),
        collected_at=NOW,
        max_summary_chars=4000,
        tone=scorer,
    )
    assert len(fed) == 3
    assert fed[0].published_at == datetime(2026, 10, 16, 15, tzinfo=UTC)
    assert fed[0].guid == fed[0].link
    assert fed[0].link.startswith("https://www.federalreserve.gov")
    assert fed[0].kind == "central_bank"
    assert fed[0].region == "us"
    assert "<item" in fed[0].raw
    boc = parse_feed(
        (FIXTURES / "rss_boc_press.rdf").read_bytes(),
        feed_id="boc_press",
        feed=feed(shipped_settings, "boc_press"),
        collected_at=NOW,
        max_summary_chars=4000,
        tone=scorer,
    )
    assert len(boc) == 2
    assert boc[0].guid == boc[0].link
    assert boc[0].published_at == datetime(2026, 9, 10, 11, 0, 9, tzinfo=UTC)
    fcx = parse_feed(
        (FIXTURES / "atom_fcx_filings.xml").read_bytes(),
        feed_id="fcx_filings",
        feed=feed(shipped_settings, "fcx_filings"),
        collected_at=NOW,
        max_summary_chars=60,
        tone=scorer,
    )
    assert [i.title for i in fcx] == ["8-K - Current report", "8-K - Current report"]
    assert fcx[0].symbol == "FCX"
    assert fcx[0].guid.endswith("0000831259-26-000043")
    assert fcx[0].link.endswith("-index.htm")
    assert fcx[0].summary is not None
    assert fcx[0].summary.startswith("Filed: 2026-10-02 AccNo:")
    assert len(fcx[0].summary) == 60
    assert fcx[0].published_at is not None
    assert fcx[0].published_at.tzinfo is UTC


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (b'<?xml version="1.0"?><!DOCTYPE rss [<!ENTITY x "y">]><rss/>', "DTD or an entity"),
        (b"<rss><channel>", "not well-formed"),
        (b"<html><body>Not Found</body></html>", "not an RSS or Atom feed"),
    ],
)
def test_a_feed_that_is_unsafe_broken_or_not_a_feed_is_refused(
    shipped_settings: ObliskConfig, body: bytes, message: str
) -> None:
    with pytest.raises(UpstreamError, match=message):
        parse_feed(
            body,
            feed_id="x",
            feed=feed(shipped_settings, "fed_press"),
            collected_at=NOW,
            max_summary_chars=100,
            tone=tone(shipped_settings),
        )


def test_an_item_s_id_falls_back_from_guid_to_link_to_title_and_date(
    shipped_settings: ObliskConfig,
) -> None:
    body = b"""<rss version="2.0"><channel>
      <item><title>  </title><link>https://a</link></item>
      <item><title>Linked</title><link>https://b</link><pubDate>someday</pubDate></item>
      <item><title>Bare</title><pubDate>Fri, 16 Oct 2026 15:00:00</pubDate>
        <description>&lt;p&gt;Rates &amp;amp; markets&lt;/p&gt;</description></item>
      <item><title>Stamped</title><dc:date xmlns:dc="http://purl.org/dc/elements/1.1/">2026-10-01</dc:date>
        <link rel="related" href="https://related"/><link href="https://alternate"/></item>
    </channel></rss>"""
    items = parse_feed(
        body,
        feed_id="x",
        feed=feed(shipped_settings, "fed_press"),
        collected_at=NOW,
        max_summary_chars=100,
        tone=tone(shipped_settings),
    )
    linked, bare, stamped = items
    assert linked.item_id == item_id(None, "https://b", "Linked", None)
    assert linked.published_at is None  # an unreadable date is absent, never guessed
    assert bare.published_at is None
    assert bare.link is None
    assert bare.item_id == item_id(None, None, "Bare", None)
    assert bare.summary == "Rates & markets"
    assert stamped.published_at is None
    assert stamped.link == "https://alternate"
    assert item_id("g", "l", "t", NOW) == item_id("g", None, "other", None)
    assert plain(None, 10) is None
    assert plain("<b> </b>", 10) is None


def test_the_fallback_tone_reads_the_financial_terms(shipped_settings: ObliskConfig) -> None:
    scorer = tone(shipped_settings)
    plain_vader = FallbackTone({})
    assert scorer.score("Equities in a selloff as default fears spread") < -0.5
    assert plain_vader.score("selloff") == 0.0
    assert (scorer.score("selloff") or 0) < 0
    assert (scorer.score("Strong profits and a buyback") or 0) > 0.5
    assert scorer.score("   ") is None


def news_settings(
    settings: ObliskConfig, feeds: dict[str, FeedConfig], *, max_age: int = 60
) -> ObliskConfig:
    """The news section with ``feeds``, reading items up to ``max_age`` days old (the
    fixtures were published days before ``NOW``)."""
    news = settings.sources.news.model_copy(update={"feeds": feeds, "max_item_age_days": max_age})
    return with_sources(settings, news=news)


FCX_FILING = (
    "https://www.sec.gov/Archives/edgar/data/831259/000083125926000043/0000831259-26-000043"
)
FCX_OLDER = "https://www.sec.gov/Archives/edgar/data/831259/000083125926000038/0000831259-26-000038"


def news_collector(
    settings: ObliskConfig,
    worker: StoreWorker,
    client: httpx.AsyncClient,
    agents: dict[str, str | None],
) -> NewsCollector:
    return NewsCollector(
        settings,
        client=client,
        policy=policy(settings),
        worker=worker,
        pacer=fast_pacer(settings),
        agents=agents,
        now=lambda: NOW,
    )


async def test_feeds_are_collected_deduplicated_and_sec_s_carry_its_agent(
    shipped_settings: ObliskConfig, store_worker: StoreWorker, store: Store
) -> None:
    fed_feed = feed(shipped_settings, "fed_press")
    mirror = fed_feed.model_copy(update={"url": "https://mirror.example/fed.xml"})
    feeds = {
        "fed_press": fed_feed,
        "fed_mirror": mirror,
        "fcx_filings": feed(shipped_settings, "fcx_filings"),
        "empty": fed_feed.model_copy(update={"url": "https://empty.example/rss"}),
    }
    agents: list[str] = []

    def sec(request: httpx.Request) -> httpx.Response:
        agents.append(request.headers["user-agent"])
        if str(request.url) == f"{FCX_FILING}.txt":
            return httpx.Response(
                200, content=(FIXTURES / "edgar_fcx_8k_submission.txt").read_bytes()
            )
        if str(request.url) == f"{FCX_OLDER}.txt":
            return httpx.Response(503, text="busy")
        return httpx.Response(200, content=(FIXTURES / "atom_fcx_filings.xml").read_bytes())

    fed_body = (FIXTURES / "rss_fed_press.xml").read_bytes()
    settings = news_settings(shipped_settings, feeds)
    async with httpx.AsyncClient() as client:
        with respx.mock as router:
            router.get(fed_feed.url).mock(return_value=httpx.Response(200, content=fed_body))
            router.get(mirror.url).mock(return_value=httpx.Response(200, content=fed_body))
            router.get(feeds["fcx_filings"].url).mock(side_effect=sec)
            router.get(url__startswith="https://www.sec.gov/Archives/").mock(side_effect=sec)
            router.get("https://empty.example/rss").mock(
                return_value=httpx.Response(200, content=b"<rss><channel/></rss>")
            )
            first = await news_collector(
                settings, store_worker, client, {"SEC_USER_AGENT": "Owner owner@example.com"}
            ).run()
            again = await news_collector(
                settings, store_worker, client, {"SEC_USER_AGENT": "Owner owner@example.com"}
            ).run()
    rows = {o.target: (o.rows, o.detail["listed"]) for o in first.outcomes}
    assert rows == {
        "fed_press": (3, 3),
        "fed_mirror": (0, 3),
        "fcx_filings": (1, 2),  # the older filing did not answer: asked again next run
        "empty": (0, 0),
    }
    outcomes = {o.target: o for o in first.outcomes}
    assert outcomes["fcx_filings"].degraded
    assert "HTTP 503" in outcomes["fcx_filings"].reason
    assert set(agents) == {"Owner owner@example.com"}  # feeds and filings alike
    assert first.status == "degraded"
    assert again.rows == 0
    # Feed, both filings; then feed and the unanswered filing again (the held one is not read).
    assert len(agents) == 5
    held = source_repo.news_items(store, as_of=NOW.date())
    assert len(held) == 4
    assert {i.feed for i in held} == {"fed_press", "fcx_filings"}
    (filing,) = [i for i in held if i.feed == "fcx_filings"]
    assert filing.summary is not None
    assert "Freeport Provides Third-Quarter 2026 Operational Update" in filing.summary
    assert "<" not in filing.summary
    assert filing.fallback_tone is not None
    assert source_repo.news_items(store, as_of=NOW.date() - timedelta(days=1)) == ()


def test_a_filing_s_press_release_is_its_first_ex_99_else_its_first_document() -> None:
    submission = (FIXTURES / "edgar_fcx_8k_submission.txt").read_text(encoding="latin-1")
    text = filing_text(submission, 200)
    assert text is not None
    assert "Third-Quarter 2026 Operational Update" in text
    assert len(text) == 200
    main_only = (
        "<DOCUMENT>\n<TYPE>6-K\n<TEXT><p>Main <b>body</b></p><script>x()</script></TEXT>"
        "\n</DOCUMENT>"
    )
    assert filing_text(main_only, 100) == "Main body"
    assert filing_text("no documents", 100) is None
    assert submission_url(f"{FCX_FILING}-index.htm") == f"{FCX_FILING}.txt"
    assert submission_url("https://example.com/item") is None
    assert submission_url(None) is None


async def test_an_old_item_is_left_out_and_an_unreadable_filing_entry_waits(
    shipped_settings: ObliskConfig, store_worker: StoreWorker, store: Store
) -> None:
    fed_feed = feed(shipped_settings, "fed_press")
    issuer = feed(shipped_settings, "fcx_filings")
    linkless = b"""<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>8-K</title>
      <id>urn:x</id><updated>2026-10-23T12:00:00-04:00</updated></entry>
      <entry><title>8-K too</title><link href="https://www.sec.gov/x-index.htm"/>
      <id>urn:y</id><updated>2026-10-23T12:00:00-04:00</updated></entry>
      <entry><title>8-K, large</title><link href="https://www.sec.gov/big-index.htm"/>
      <id>urn:z</id><updated>2026-10-23T12:00:00-04:00</updated></entry></feed>"""
    shipped = news_settings(shipped_settings, {"fed_press": fed_feed, "fcx": issuer}, max_age=3)
    news = shipped.sources.news.model_copy(update={"max_body_bytes": 3000})
    settings = with_sources(shipped, news=news)
    async with httpx.AsyncClient() as client:
        with respx.mock as router:
            router.get(fed_feed.url).mock(
                return_value=httpx.Response(
                    200, content=(FIXTURES / "rss_fed_press.xml").read_bytes()
                )
            )
            router.get(issuer.url).mock(return_value=httpx.Response(200, content=linkless))
            router.get("https://www.sec.gov/x.txt").mock(
                return_value=httpx.Response(200, text="no documents here")
            )
            router.get("https://www.sec.gov/big.txt").mock(
                return_value=httpx.Response(200, text="<DOCUMENT>" + " " * 3500)
            )
            result = await news_collector(
                settings, store_worker, client, {"SEC_USER_AGENT": "Owner owner@example.com"}
            ).run()
    outcomes = {o.target: o for o in result.outcomes}
    assert outcomes["fed_press"].rows == 0  # published a week before: left out
    assert outcomes["fed_press"].detail["old"] == 3
    assert not outcomes["fed_press"].degraded
    assert outcomes["fcx"].rows == 0
    assert "links to no filing index" in outcomes["fcx"].reason
    assert "holds no document" in outcomes["fcx"].reason
    assert "over the 3000 bytes" in outcomes["fcx"].reason


async def test_a_feed_without_its_agent_too_large_or_failing_fails_alone(
    shipped_settings: ObliskConfig, store_worker: StoreWorker, store: Store
) -> None:
    fed_feed = feed(shipped_settings, "fed_press")
    feeds = {
        "fcx_filings": feed(shipped_settings, "fcx_filings"),
        "huge": fed_feed.model_copy(update={"url": "https://huge.example/rss"}),
        "down": fed_feed.model_copy(update={"url": "https://down.example/rss"}),
        "fed_press": fed_feed,
    }
    news = shipped_settings.sources.news.model_copy(update={"feeds": feeds, "max_body_bytes": 4000})
    settings = with_sources(shipped_settings, news=news)
    async with httpx.AsyncClient() as client:
        with respx.mock as router:
            router.get("https://huge.example/rss").mock(
                return_value=httpx.Response(200, content=b"<rss>" + b" " * 5000 + b"</rss>")
            )
            router.get("https://down.example/rss").mock(return_value=httpx.Response(404))
            router.get(fed_feed.url).mock(
                return_value=httpx.Response(
                    200, content=(FIXTURES / "rss_fed_press.xml").read_bytes()
                )
            )
            result = await news_collector(
                settings, store_worker, client, {"SEC_USER_AGENT": None}
            ).run(dry_run=True)
    failed = {o.target: o.reason for o in result.outcomes if not o.ok}
    assert "SEC_USER_AGENT is unset" in failed["fcx_filings"]
    assert "over the 4000 allowed" in failed["huge"]
    assert "HTTP 404" in failed["down"]
    assert result.status == "partial"
    assert source_repo.news_items(store, as_of=NOW.date()) == ()


# ── earnings ──


def stamp(days: float) -> datetime:
    return NOW + timedelta(days=days)


def test_the_recent_past_reports_and_the_upcoming_ones_within_the_horizon_are_kept() -> None:
    listed = [stamp(-400), stamp(-300), stamp(-200), stamp(-100), stamp(4), stamp(95), stamp(200)]
    kept = select_dates(listed, now=NOW, past_reports=2, horizon_days=120)
    assert kept == [stamp(-200), stamp(-100), stamp(4), stamp(95)]
    assert select_dates(listed, now=NOW, past_reports=0, horizon_days=120) == [stamp(4), stamp(95)]
    with pytest.raises(UpstreamError, match="time zone"):
        select_dates([datetime(2026, 10, 27, 8)], now=NOW, past_reports=2, horizon_days=120)


class FakeYahoo:
    def __init__(self, listed: dict[str, Sequence[datetime]]) -> None:
        self.listed = listed
        self.asked: list[tuple[str, int]] = []

    def __call__(self, symbol: str, limit: int) -> Sequence[datetime]:
        self.asked.append((symbol, limit))
        if symbol == "ALB":
            raise ValueError("Yahoo answered nothing usable")
        return self.listed.get(symbol, [])


def earnings_collector(
    settings: ObliskConfig, worker: StoreWorker, fetch: FakeYahoo, now: datetime
) -> EarningsCollector:
    return EarningsCollector(
        settings, worker=worker, pacer=fast_pacer(settings), fetch=fetch, now=lambda: now
    )


async def test_earnings_are_read_per_stock_refreshed_when_stale_and_read_as_of(
    shipped_settings: ObliskConfig, store_worker: StoreWorker, store: Store
) -> None:
    yahoo = FakeYahoo({"FCX": [stamp(-90), stamp(4)], "ASML.AS": [stamp(-80), stamp(10)]})
    first = await earnings_collector(shipped_settings, store_worker, yahoo, NOW).run()
    outcomes = {o.target: o for o in first.outcomes}
    assert outcomes["FCX"].rows == 2
    assert outcomes["FCX"].detail["next"] == "2026-10-27"
    assert not outcomes["ALB"].ok
    assert "nothing usable" in outcomes["ALB"].reason
    assert outcomes["MOS"].degraded
    assert outcomes["MOS"].reason == "Yahoo listed no earnings date"
    assert ("FCX", shipped_settings.sources.catalysts.earnings.request_limit) in yahoo.asked
    held = source_repo.earnings_dates(store, "FCX", as_of=NOW.date())
    assert [d.report_at for d in held] == [stamp(-90), stamp(4)]
    assert {d.grade for d in held} == {"research"}
    assert held[0].source == "yahoo"
    assert source_repo.earnings_dates(store, "FCX", as_of=NOW.date() - timedelta(days=1)) == ()

    soon = NOW + timedelta(days=2)
    fresh = await earnings_collector(shipped_settings, store_worker, yahoo, soon).run()
    assert fresh.notes["fresh"] == 2
    assert "FCX" not in {o.target for o in fresh.outcomes}
    forced = await earnings_collector(shipped_settings, store_worker, yahoo, soon).run(
        force=True, dry_run=True
    )
    assert {o.target for o in forced.outcomes} >= {"FCX", "ASML"}
    assert source_repo.earnings_collected_on(store, "FCX") == NOW.date()


async def test_a_stock_without_a_yahoo_symbol_and_an_empty_universe_are_said_so(
    shipped_settings: ObliskConfig, store_worker: StoreWorker
) -> None:
    yahoo_config = shipped_settings.research.data.yahoo
    suffixes = {k: v for k, v in yahoo_config.suffixes.items() if k != "BM"}
    data = shipped_settings.research.data.model_copy(
        update={"yahoo": yahoo_config.model_copy(update={"suffixes": suffixes})}
    )
    research = shipped_settings.research.model_copy(update={"data": data})
    settings = shipped_settings.model_copy(update={"research": research})
    result = await earnings_collector(settings, store_worker, FakeYahoo({}), NOW).run(dry_run=True)
    santander = {o.target: o for o in result.outcomes}["SAN"]
    assert santander.degraded
    assert santander.reason == "no Yahoo suffix for BM"
    universe = shipped_settings.universe.model_copy(update={"instruments": []})
    bare = shipped_settings.model_copy(update={"universe": universe})
    empty = await earnings_collector(bare, store_worker, FakeYahoo({}), NOW).run()
    assert empty.judge().reason == "the universe holds no stock"


async def test_a_run_with_every_stock_fresh_is_skipped_with_the_reason(
    shipped_settings: ObliskConfig, store_worker: StoreWorker
) -> None:
    fcx = [i for i in shipped_settings.universe.instruments if i.symbol == "FCX"]
    universe = shipped_settings.universe.model_copy(update={"instruments": fcx})
    settings = shipped_settings.model_copy(update={"universe": universe})
    yahoo = FakeYahoo({"FCX": [stamp(4)]})
    await earnings_collector(settings, store_worker, yahoo, NOW).run()
    fresh = await earnings_collector(settings, store_worker, yahoo, NOW + timedelta(days=6)).run()
    assert fresh.status == "skipped"
    assert fresh.judge().reason == "every stock was read within 7 days"
    stale = await earnings_collector(settings, store_worker, yahoo, NOW + timedelta(days=7)).run()
    assert [o.target for o in stale.outcomes] == ["FCX"]
