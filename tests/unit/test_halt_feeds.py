"""The single-stock halt feed: the structured-feed reader ported from market-pulse-bot, the Nasdaq
Trader halts read into instrument incidents (never a venue's), and the refresh that keeps the last
state when the feed fails, restores it after a restart and alerts after repeated failures.

Ported in intent from market-pulse-bot's ``halt_detector`` tests: structured items from JSON or
XML, a single-stock feed never changes a market's state.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx

from obliskiq.calendars.halt_feeds import (
    instrument_key,
    nasdaq_trader_incidents,
    structured_items,
)
from obliskiq.calendars.service import Calendars
from obliskiq.calendars.status import BlockReason
from obliskiq.config import ObliskConfig
from obliskiq.data.halts import HaltFeed, followed_conids
from obliskiq.domain.calendar import IncidentScope, IncidentSource
from obliskiq.errors import UpstreamError
from obliskiq.store import Store
from obliskiq.store import sources as source_repo
from obliskiq.store.worker import StoreWorker
from obliskiq.util.http import RetryPolicy

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "halts" / "nasdaq_trader_halts.xml"
EASTERN = ZoneInfo("America/New_York")
FEED = "https://www.nasdaqtrader.com/rss.aspx?feed=tradehalts"
ORIGIN = "nasdaq-trader-halts"
NOW = datetime(2026, 10, 23, 14, 0, tzinfo=UTC)
NIVFW, SVA, FCX = 111, 222, 7089
CONIDS = {("XNAS", "NIVFW"): NIVFW, ("XNAS", "SVA"): SVA}
MARKETS = {"NASDAQ": "XNAS", "NYSE": "XNYS"}


def resumed_item(symbol: str = "FCX", market: str = "NYSE") -> bytes:
    return f"""<rss xmlns:ndaq="http://www.nasdaqtrader.com/"><channel><item>
      <ndaq:HaltDate>10/23/2026</ndaq:HaltDate><ndaq:HaltTime>09:45:00</ndaq:HaltTime>
      <ndaq:IssueSymbol>{symbol}</ndaq:IssueSymbol><ndaq:IssueName>Freeport</ndaq:IssueName>
      <ndaq:Market>{market}</ndaq:Market><ndaq:ReasonCode>LUDP</ndaq:ReasonCode>
      <ndaq:ResumptionDate>10/23/2026</ndaq:ResumptionDate>
      <ndaq:ResumptionTradeTime>09:50:00</ndaq:ResumptionTradeTime>
    </item></channel></rss>""".encode()


# ── reading the feed ──


def test_structured_items_are_read_from_xml_with_a_byte_order_mark() -> None:
    items = structured_items(FIXTURE.read_bytes())
    assert [i["IssueSymbol"] for i in items] == ["NIVFW", "SABSW", "ZONE", "SVA"]
    assert items[0]["HaltTime"] == "19:50:00.000"
    assert items[0]["ResumptionDate"] == ""


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (b'[{"IssueSymbol": "A", "Market": null}]', [{"IssueSymbol": "A", "Market": ""}]),
        (b'{"items": [{"IssueSymbol": "B"}]}', [{"IssueSymbol": "B"}]),
        (b'{"data": [{"IssueSymbol": "C"}]}', [{"IssueSymbol": "C"}]),
    ],
)
def test_structured_items_are_read_from_json_as_pulse_did(
    content: bytes, expected: list[dict[str, str]]
) -> None:
    assert structured_items(content) == expected


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (b'{"items": "none"}', "no list of items"),
        (b"[1, 2]", "no list of items"),
        (b'<!DOCTYPE x [<!ENTITY a "b">]><rss/>', "DTD or an entity"),
        (b"<html><body>", "neither JSON nor well-formed XML"),
    ],
)
def test_a_feed_that_is_not_a_list_of_items_or_is_unsafe_is_refused(
    content: bytes, message: str
) -> None:
    with pytest.raises(UpstreamError, match=message):
        structured_items(content)


def test_only_the_instruments_we_follow_become_instrument_incidents() -> None:
    read = nasdaq_trader_incidents(
        structured_items(FIXTURE.read_bytes()),
        origin=ORIGIN,
        markets=MARKETS,
        zone=EASTERN,
        conids=CONIDS,
    )
    assert read.listed == 4
    assert dict(read.left_out) == {"instrument not followed": 1, "market not followed": 1}
    first, sinovac = read.incidents
    assert first.scope is IncidentScope.INSTRUMENT
    assert first.conid == NIVFW
    assert first.venue == "XNAS"
    assert first.source is IncidentSource.FEED
    assert first.start == datetime(2026, 10, 22, 23, 50, tzinfo=UTC)  # 19:50 New York, EDT
    assert first.expected_end is None  # no resumption shown: open-ended
    assert first.incident_id == "NIVFW@XNAS:20261022T235000Z"
    assert "T1" in first.reason
    assert first.origin == ORIGIN
    assert sinovac.start == datetime(2019, 2, 22, 21, 2, 1, tzinfo=UTC)  # EST, no milliseconds


def test_a_resumption_ends_the_halt_and_a_bad_one_leaves_it_open() -> None:
    conids = {("XNYS", "FCX"): FCX}
    (resumed,) = nasdaq_trader_incidents(
        structured_items(resumed_item()),
        origin=ORIGIN,
        markets=MARKETS,
        zone=EASTERN,
        conids=conids,
    ).incidents
    assert resumed.expected_end == datetime(2026, 10, 23, 13, 50, tzinfo=UTC)
    early = resumed_item().replace(b"09:50:00</ndaq:Resumption", b"09:40:00</ndaq:Resumption")
    (open_ended,) = nasdaq_trader_incidents(
        structured_items(early), origin=ORIGIN, markets=MARKETS, zone=EASTERN, conids=conids
    ).incidents
    assert open_ended.expected_end is None  # a resumption before the halt is not believed
    unreadable = resumed_item().replace(b"10/23/2026</ndaq:HaltDate>", b"tomorrow</ndaq:HaltDate>")
    read = nasdaq_trader_incidents(
        structured_items(unreadable), origin=ORIGIN, markets=MARKETS, zone=EASTERN, conids=conids
    )
    assert read.incidents == ()
    assert dict(read.left_out) == {"unreadable halt time": 1}
    clockless = resumed_item().replace(b"09:45:00</ndaq:HaltTime>", b"9h45</ndaq:HaltTime>")
    assert not nasdaq_trader_incidents(
        structured_items(clockless), origin=ORIGIN, markets=MARKETS, zone=EASTERN, conids=conids
    ).incidents


def test_instrument_keys_ignore_punctuation_and_case() -> None:
    assert instrument_key("BRK B") == instrument_key("brk.b") == "BRKB"


# ── the refresh ──


def halt_settings(settings: ObliskConfig) -> ObliskConfig:
    fcx = next(i for i in settings.universe.instruments if i.symbol == "FCX")
    instruments = [
        fcx,
        fcx.model_copy(update={"symbol": "NIVFW", "listing_exchange": "NASDAQ", "conid": NIVFW}),
        fcx.model_copy(update={"symbol": "SVA", "listing_exchange": "NASDAQ", "conid": SVA}),
        fcx.model_copy(update={"symbol": "UNPINNED", "listing_exchange": "NASDAQ", "conid": None}),
    ]
    universe = settings.universe.model_copy(update={"instruments": instruments})
    return settings.model_copy(update={"universe": universe})


def halt_feed(
    settings: ObliskConfig,
    calendars: Calendars,
    worker: StoreWorker,
    client: httpx.AsyncClient,
    now: Callable[[], datetime] = lambda: NOW,
) -> HaltFeed:
    return HaltFeed(
        settings,
        calendars=calendars,
        client=client,
        policy=RetryPolicy.from_settings(settings.http.model_copy(update={"retries": 0})),
        worker=worker,
        now=now,
    )


def test_the_followed_instruments_are_the_pinned_ones_on_the_feed_s_venues(
    shipped_settings: ObliskConfig,
) -> None:
    conids = followed_conids(halt_settings(shipped_settings), {"XNAS", "XNYS"})
    assert conids == {("XNYS", "FCX"): FCX, ("XNAS", "NIVFW"): NIVFW, ("XNAS", "SVA"): SVA}


async def test_a_halt_blocks_its_instrument_never_its_venue_and_is_kept_and_restored(
    shipped_settings: ObliskConfig,
    build_calendars: Callable[..., Calendars],
    store_worker: StoreWorker,
    store: Store,
) -> None:
    settings = halt_settings(shipped_settings)
    calendars = build_calendars(("XNYS", "XNAS"))
    async with httpx.AsyncClient() as client:
        with respx.mock as router:
            router.get(FEED).mock(return_value=httpx.Response(200, content=FIXTURE.read_bytes()))
            feed = halt_feed(settings, calendars, store_worker, client)
            assert feed.due(NOW)
            refreshed = await feed.refresh()
            assert not feed.due(NOW + timedelta(minutes=4))
            assert feed.due(NOW + timedelta(minutes=5))
    assert refreshed.ok
    assert refreshed.incidents == 2
    assert refreshed.read is not None
    blocked = calendars.venue_status("XNAS", NOW.date(), conid=NIVFW)
    assert blocked.reason is BlockReason.INCIDENT_OVERLAP
    assert calendars.venue_status("XNAS", NOW.date(), conid=FCX).reason is not (
        BlockReason.INCIDENT_OVERLAP
    )  # the venue and its other instruments trade on
    fetched = source_repo.halt_state(store, ORIGIN)
    assert fetched is not None
    assert fetched[0] == NOW
    assert "NIVFW@XNAS" in fetched[1]

    fresh = build_calendars(("XNYS", "XNAS"))
    async with httpx.AsyncClient() as client:
        restored = await halt_feed(settings, fresh, store_worker, client).restore()
    assert restored == 2
    assert fresh.venue_status("XNAS", NOW.date(), conid=SVA).reason is (
        BlockReason.INCIDENT_OVERLAP
    )


async def test_a_failing_feed_keeps_its_last_state_and_alerts_after_repeated_failures(
    shipped_settings: ObliskConfig,
    build_calendars: Callable[..., Calendars],
    store_worker: StoreWorker,
) -> None:
    settings = halt_settings(shipped_settings)
    calendars = build_calendars(("XNYS", "XNAS"))
    answers = iter(
        [
            httpx.Response(200, content=FIXTURE.read_bytes()),
            httpx.Response(503, text="busy"),
            httpx.Response(200, text="<html>maintenance"),
            httpx.Response(200, content=b"<!DOCTYPE x><rss/>"),
            httpx.Response(200, content=b"<rss><channel/></rss>"),
        ]
    )
    async with httpx.AsyncClient() as client:
        with respx.mock as router:
            router.get(FEED).mock(side_effect=lambda _request: next(answers))
            feed = halt_feed(settings, calendars, store_worker, client)
            results = [await feed.refresh() for _ in range(5)]
    assert [r.ok for r in results] == [True, False, False, False, True]
    assert [r.failures for r in results] == [0, 1, 2, 3, 0]
    assert [r.alert for r in results] == [False, False, False, True, False]
    assert results[1].incidents == 2  # the halts stay in force while the feed is down
    assert "HTTP 503" in (results[1].error or "")
    assert results[4].incidents == 0  # a good read with no halt lifts them


async def test_a_resumed_halt_lapses_after_the_grace_and_a_corrupt_state_restores_nothing(
    shipped_settings: ObliskConfig,
    build_calendars: Callable[..., Calendars],
    store_worker: StoreWorker,
    store: Store,
) -> None:
    settings = halt_settings(shipped_settings)
    calendars = build_calendars(("XNYS", "XNAS"))
    resumption = datetime(2026, 10, 23, 13, 50, tzinfo=UTC)
    grace = timedelta(minutes=settings.calendars.incidents.grace_minutes)
    async with httpx.AsyncClient() as client:
        with respx.mock as router:
            router.get(FEED).mock(return_value=httpx.Response(200, content=resumed_item()))
            within = await halt_feed(
                settings, calendars, store_worker, client, lambda: resumption + grace
            ).refresh()
            after = await halt_feed(
                settings, calendars, store_worker, client, lambda: resumption + grace * 2
            ).refresh()
    assert within.incidents == 1
    assert after.incidents == 0
    source_repo.save_halt_state(store, ORIGIN, fetched_at=NOW, payload="not json")
    async with httpx.AsyncClient() as client:
        assert await halt_feed(settings, calendars, store_worker, client).restore() == 0
    async with httpx.AsyncClient() as client:
        empty = build_calendars(("XNYS", "XNAS"))
        source_repo.save_halt_state(store, ORIGIN, fetched_at=NOW, payload="[]")
        assert await halt_feed(settings, empty, store_worker, client).restore() == 0


async def test_a_venue_the_calendars_do_not_cover_is_not_mapped(
    shipped_settings: ObliskConfig,
    build_calendars: Callable[..., Calendars],
    store_worker: StoreWorker,
) -> None:
    settings = halt_settings(shipped_settings)
    calendars = build_calendars(("XNYS",))
    async with httpx.AsyncClient() as client:
        with respx.mock as router:
            router.get(FEED).mock(return_value=httpx.Response(200, content=FIXTURE.read_bytes()))
            feed = halt_feed(settings, calendars, store_worker, client)
            assert await feed.restore() == 0  # nothing held yet
            result = await feed.refresh()
    assert result.ok
    assert result.incidents == 0
    assert result.read is not None
    assert result.read.left_out["market not followed"] == 4  # Nasdaq and NYSE American


async def test_a_restored_state_keeps_only_mapped_venues_and_a_refused_one_restores_nothing(
    shipped_settings: ObliskConfig,
    build_calendars: Callable[..., Calendars],
    store_worker: StoreWorker,
    store: Store,
) -> None:
    settings = halt_settings(shipped_settings)
    calendars = build_calendars(("XNYS", "XNAS"))
    async with httpx.AsyncClient() as client:
        with respx.mock as router:
            router.get(FEED).mock(return_value=httpx.Response(200, content=FIXTURE.read_bytes()))
            await halt_feed(settings, calendars, store_worker, client).refresh()
        narrower = build_calendars(("XNYS",))  # XNAS no longer covered: its halts are left out
        assert await halt_feed(settings, narrower, store_worker, client).restore() == 0
        held = source_repo.halt_state(store, ORIGIN)
        assert held is not None
        foreign = held[1].replace(f'"origin": "{ORIGIN}"', '"origin": "another-feed"')
        assert foreign != held[1]
        source_repo.save_halt_state(store, ORIGIN, fetched_at=NOW, payload=foreign)
        fresh = build_calendars(("XNYS", "XNAS"))
        assert await halt_feed(settings, fresh, store_worker, client).restore() == 0  # refused
    assert fresh.venue_status("XNAS", NOW.date(), conid=NIVFW).reason is not (
        BlockReason.INCIDENT_OVERLAP
    )
