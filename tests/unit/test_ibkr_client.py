"""The typed read client, endpoint group by endpoint group, against respx-mocked responses.

Ported from ObliskIQ-lite-MCP's ``tests/test_cp_client.py`` (the endpoint half), adapted to
:class:`~obliskiq.ibkr.client.IbkrClient`. A fixture is evidence about our parsing, never about
IBKR's format: the shapes here are the ones lite measured, tagged where they were.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import ast
import inspect
import json
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from obliskiq.domain.enums import MarketDataStatus
from obliskiq.ibkr.client import IbkrClient, MarketData, Secdef
from obliskiq.ibkr.errors import AccountGuardError
from obliskiq.ibkr.guard import AccountGuard
from obliskiq.ibkr.models import ContractMatch
from obliskiq.ibkr.session import BrokerSession

BASE = "https://gw.test/v1/api"
API_PREFIX = "/v1/api/"
SEARCH = "iserver/secdef/search"
SNAPSHOT = "iserver/marketdata/snapshot"

#: A placeholder paper account id; not shaped like a real one.
ACCOUNT = "DUTEST1"

IBKR_PACKAGE = Path(__file__).resolve().parents[2] / "src" / "obliskiq" / "ibkr"

type Handler = Callable[[httpx.Request], httpx.Response]


def mocked() -> respx.MockRouter:
    return respx.mock(base_url=BASE, assert_all_called=False)


def rel(request: httpx.Request) -> str:
    return request.url.path.removeprefix(API_PREFIX)


def wire(mock: respx.MockRouter, handler: Handler) -> respx.Route:
    return mock.route().mock(side_effect=handler)


def with_section(ibkr: Any, section: str, **values: Any) -> Any:
    """``ibkr`` with ``values`` replaced in one of its sub-models."""
    return ibkr.model_copy(update={section: getattr(ibkr, section).model_copy(update=values)})


@pytest.fixture
def guard() -> AccountGuard:
    return AccountGuard(account_id=ACCOUNT, profile="paper", paper_prefixes=("DU",))


@pytest.fixture
def session(make_session: Callable[..., BrokerSession]) -> BrokerSession:
    return make_session()


@pytest.fixture
def client(session, shipped_settings, guard) -> IbkrClient:
    return IbkrClient(session, shipped_settings, guard)


def secdef_with(make_session, ibkr: Any) -> Secdef:
    return Secdef(make_session(ibkr=ibkr), ibkr)


def marketdata_with(make_session, ibkr: Any) -> MarketData:
    return MarketData(make_session(ibkr=ibkr), ibkr)


# ── /iserver/secdef/search and the resolution rules ──

#: The API ranks by its own relevance; the wanted ticker is NOT first.
SEARCH_BP = [
    {"conid": 111, "symbol": "BPAQF", "companyName": "BP p.l.c. - unsponsored ADR"},
    {"conid": 222, "symbol": "BP", "companyName": "BP p.l.c."},
    {"conid": 333, "symbol": "BPT", "companyName": "BP Prudhoe Bay Royalty Trust"},
]

#: A dual-listed search: EVERY row answers the bare ticker; the venue is in `description`.
#: Ordering and venues are ASML's [CP ✓ 2026-09-10].
SEARCH_ASML = [
    {"conid": 117902840, "symbol": "ASML", "description": "NASDAQ"},
    {"conid": 762277717, "symbol": "ASML", "description": "TSE"},
    {"conid": 117589399, "symbol": "ASML", "description": "AEB"},
]


async def search_and_pick(
    secdef: Secdef, rows: Any, ticker: str, *, exchange: str | None = None
) -> list[int]:
    with mocked() as mock:
        mock.post(SEARCH).respond(200, json=rows)
        matches = await secdef.search(ticker)
    return [match.conid for match in secdef.pick(matches, ticker, exchange=exchange)]


async def test_exact_ticker_match_beats_the_apis_own_ordering(client):
    """secdef/search ranks by relevance, which is not correctness."""
    assert await search_and_pick(client.secdef, SEARCH_BP, "BP") == [222]


async def test_exact_ticker_match_is_case_insensitive(client):
    assert await search_and_pick(client.secdef, SEARCH_BP, "bp") == [222]
    assert await search_and_pick(client.secdef, SEARCH_BP, " Bp ") == [222]


async def test_no_exact_match_resolves_to_nothing_under_the_shipped_config(
    client, shipped_settings
):
    """lite took the first row; an ambiguous resolution is refused now."""
    assert shipped_settings.ibkr.secdef.accept_first_on_no_exact_match is False
    assert await search_and_pick(client.secdef, SEARCH_BP, "BPX") == []


async def test_the_first_row_fallback_can_be_switched_back_on(make_session, shipped_settings):
    ibkr = with_section(shipped_settings.ibkr, "secdef", accept_first_on_no_exact_match=True)
    secdef = secdef_with(make_session, ibkr)
    assert await search_and_pick(secdef, SEARCH_BP, "BPX") == [111]
    assert await search_and_pick(secdef, SEARCH_BP, "BP") == [222], "exact still wins"


async def test_without_exact_ticker_first_every_row_survives_in_api_order(
    make_session, shipped_settings
):
    ibkr = with_section(shipped_settings.ibkr, "secdef", exact_ticker_match_first=False)
    secdef = secdef_with(make_session, ibkr)
    assert await search_and_pick(secdef, SEARCH_BP, "BP") == [111, 222, 333]


async def test_the_exact_ticker_rule_cannot_separate_one_companys_listings(client):
    """Every listing answers "ASML": without the venue, all three survive with NASDAQ first.
    lite collected New York's chain against an Amsterdam listing this way for a session."""
    assert await search_and_pick(client.secdef, SEARCH_ASML, "ASML") == [
        117902840,
        762277717,
        117589399,
    ]


async def test_the_declared_venue_picks_the_declared_listing(client):
    """Verified in lite [CP ✓ 2026-09-10] on ASML/AEB, SAN/BM and TTE/SBF."""
    assert await search_and_pick(client.secdef, SEARCH_ASML, "ASML", exchange="AEB") == [117589399]


async def test_the_declared_venue_is_matched_case_insensitively(client):
    assert await search_and_pick(client.secdef, SEARCH_ASML, "ASML", exchange="aeb") == [117589399]


async def test_a_declared_venue_that_matches_nothing_falls_through_to_the_ticker_rule(client):
    """A tie-break, not a filter: a venue the search never mentions cannot turn a working
    resolution into a failure."""
    picked = await search_and_pick(client.secdef, SEARCH_ASML, "ASML", exchange="NOSUCH")
    assert picked == [117902840, 762277717, 117589399]


async def test_the_venue_never_overrides_the_ticker_rule(client):
    """A row on the right venue under the WRONG ticker is a different company."""
    rows = [
        {"conid": 1, "symbol": "OTHER", "description": "AEB"},
        {"conid": 2, "symbol": "ASML", "description": "NASDAQ"},
    ]
    assert await search_and_pick(client.secdef, rows, "ASML", exchange="AEB") == [2]


async def test_no_declared_venue_keeps_the_ticker_resolution(client):
    assert await search_and_pick(client.secdef, SEARCH_BP, "BP", exchange=None) == [222]
    assert await search_and_pick(client.secdef, SEARCH_BP, "BP", exchange="") == [222]


async def test_no_results_resolve_to_nothing(client):
    assert await search_and_pick(client.secdef, [], "NOSUCHTHING") == []


async def test_search_rows_without_a_conid_are_skipped(client):
    rows = [{"symbol": "X"}, {"conid": 0, "symbol": "X"}, {"conid": 5, "symbol": "X"}]
    assert await search_and_pick(client.secdef, rows, "X") == [5]


async def test_a_search_row_that_is_not_an_object_is_skipped_not_decoded(client):
    with mocked() as mock:
        mock.post(SEARCH).respond(200, json=["nonsense", 7, {"conid": "1", "symbol": "OK"}])
        matches = await client.secdef.search("OK")
    assert [match.conid for match in matches] == [1]


async def test_a_search_miss_answered_as_an_object_is_no_matches(client):
    """The route answers a LIST on a hit and an OBJECT on a miss, both HTTP 200: measured on
    TOPIX [CP ✓ 2026-09-10]. Iterating the object crashed lite on its keys."""
    with mocked() as mock:
        mock.post(SEARCH).respond(200, json={"error": "No symbol found"})
        assert await client.secdef.search("TOPIX", sec_type="IND") == []
        assert await client.secdef.search("TOPIX") == []


async def test_search_sends_its_terms_as_a_json_body_not_a_query_string(client):
    """Sent as query parameters the endpoint still answers 200 and matches the symbol, but every
    row comes back with ``sections: []`` [CP ✓ 2026-09-10]: lite's first live run reported "no
    option months" for all 24 chain targets."""
    with mocked() as mock:
        route = mock.post(SEARCH).respond(200, json=[])
        await client.secdef.search("SPY", sec_type="STK")
    request = route.calls.last.request
    assert request.method == "POST"
    assert dict(request.url.params) == {}, "search terms in the query string come back sectionless"
    assert json.loads(request.content) == {"symbol": "SPY", "name": False}


async def test_search_sends_sec_type_only_when_configured(make_session, shipped_settings):
    """IBKR removed ``secType`` from the endpoint (changelog 2026-09-09)."""
    assert shipped_settings.ibkr.secdef.send_sec_type is False
    secdef = secdef_with(
        make_session, with_section(shipped_settings.ibkr, "secdef", send_sec_type=True)
    )
    with mocked() as mock:
        route = mock.post(SEARCH).respond(200, json=[])
        await secdef.search("SPY", sec_type="STK")
    assert json.loads(route.calls.last.request.content) == {
        "symbol": "SPY",
        "name": False,
        "secType": "STK",
    }


async def test_search_filters_rows_by_the_wanted_section_type(client):
    rows = [
        {"conid": 1, "symbol": "SPY", "sections": [{"secType": "STK"}, {"secType": "OPT"}]},
        {"conid": 2, "symbol": "SPY", "sections": [{"secType": "IND"}]},
        {"conid": 3, "symbol": "SPY", "sections": [{"secType": "opt"}]},
        {"conid": 4, "symbol": "SPY"},
    ]
    with mocked() as mock:
        mock.post(SEARCH).respond(200, json=rows)
        options = await client.secdef.search("SPY", sec_type="opt")
        everything = await client.secdef.search("SPY")
    assert [match.conid for match in options] == [1, 3]
    assert [match.conid for match in everything] == [1, 2, 3, 4]


async def test_search_exposes_option_months_and_exchanges_split_on_both_separators(client):
    """Both lists are ';' live [CP ✓ 2026-09-10]; ',' is accepted too."""
    rows = [
        {
            "conid": 8314,
            "symbol": "IBM",
            "companyName": "INTL BUSINESS MACHINES CORP",
            "description": "NYSE",
            "sections": [
                {"secType": "STK"},
                {"secType": "OPT", "months": "JAN26;FEB26,MAR26", "exchange": "SMART,CBOE;AMEX"},
            ],
        }
    ]
    with mocked() as mock:
        mock.post(SEARCH).respond(200, json=rows)
        [match] = await client.secdef.search("IBM")
    assert isinstance(match, ContractMatch)
    assert match.has_options
    option = match.section("OPT")
    assert option is not None
    assert option.months == ("JAN26", "FEB26", "MAR26")
    assert option.exchanges == ("SMART", "CBOE", "AMEX")
    assert match.company_name == "INTL BUSINESS MACHINES CORP"
    assert match.description == "NYSE"


async def test_bond_issuers_and_funds_search_by_name_with_their_section(client):
    rows = [
        {
            "conid": 9,
            "symbol": "T",
            "sections": [{"secType": "BOND"}],
            "issuers": [{"id": "e1", "name": "US Treasury"}, {"name": "no id"}],
        },
        {"conid": 10, "symbol": "VFIAX", "sections": [{"secType": "FUND"}]},
    ]
    with mocked() as mock:
        route = mock.post(SEARCH).respond(200, json=rows)
        bonds = await client.secdef.bond_issuers("treasury")
        funds = await client.secdef.funds("vanguard")
    assert [match.conid for match in bonds] == [9]
    assert [issuer.issuer_id for issuer in bonds[0].issuers] == ["e1"]
    assert [match.conid for match in funds] == [10]
    assert json.loads(route.calls[0].request.content)["name"] is True


async def test_bond_filters_ask_for_the_issuer_and_decode_the_options(client):
    body = {
        "bondFilters": [
            {"displayText": "Maturity Date", "options": [{"value": "2030"}, {"text": "x"}]},
            {"options": [{"value": "orphan"}]},
        ]
    }
    with mocked() as mock:
        route = mock.get("iserver/secdef/bond-filters").respond(200, json=body)
        filters = await client.secdef.bond_filters("e1")
    assert dict(route.calls.last.request.url.params) == {"symbol": "BOND", "issuerId": "e1"}
    assert [(f.name, f.values) for f in filters] == [("Maturity Date", ("2030",))]


async def test_contract_info_decodes_the_contract(client):
    body = {
        "con_id": 265598,
        "symbol": "AAPL",
        "instrument_type": "STK",
        "exchange": "SMART",
        "valid_exchanges": "SMART,AMEX;NYSE",
        "currency": "USD",
        "company_name": "APPLE INC",
        "multiplier": "1",
        "underlying_con_id": 0,
    }
    with mocked() as mock:
        mock.get("iserver/contract/265598/info").respond(200, json=body)
        info = await client.secdef.info(265598)
    assert info.conid == 265598
    assert info.symbol == "AAPL"
    assert info.sec_type == "STK"
    assert info.valid_exchanges == ("SMART", "AMEX", "NYSE")
    assert info.currency == "USD"
    assert info.multiplier == 1.0
    assert info.underlying_conid is None, "a zero underlying is no underlying"


# ── /iserver/marketdata/snapshot ──

#: What a cold subscription returns before it quotes, measured in lite [CP ✓ 2026-09-15]: the
#: status and all four Greeks about 1.3 s in, no price for another five seconds.
COLD_SKELETON = {
    "conid": 265598,
    "6509": "ZBd",
    "7308": "0.51",
    "7309": "0.02",
    "7310": "0.11",
    "7311": "-0.03",
    "_updated": 1,
}
PRICED_ROW = {"conid": 265598, "31": "228.50", "84": "228.45", "86": "228.55", "6509": "ZBd"}


def snapshot_sequence(*bodies: Any) -> tuple[list[str], Handler]:
    """Answer the bodies in turn (the last one repeats), recording the conids asked for."""
    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(request.url.params["conids"])
        return httpx.Response(200, json=bodies[min(len(asked), len(bodies)) - 1])

    return asked, handler


async def test_snapshot_polls_until_a_price_field_settles(client, fake_clock, shipped_settings):
    """The first call for an unsubscribed conid answers a skeleton; lite's original returned
    the skeleton as if it were a quote."""
    asked, handler = snapshot_sequence([COLD_SKELETON], [PRICED_ROW])
    with mocked() as mock:
        wire(mock, handler)
        quotes = await client.marketdata.snapshot([265598])
    assert len(asked) == 2
    assert quotes[265598].last == 228.50
    assert quotes[265598].bid == 228.45
    assert quotes[265598].ask == 228.55
    delay = shipped_settings.ibkr.marketdata.snapshot_settle_delay_seconds
    assert fake_clock.slept.count(delay) == 1


async def test_a_status_and_greeks_skeleton_does_not_end_the_poll(client):
    """The defect that cost lite's Phase 8 its first pass: the poll broke on ANY requested field,
    and what arrives first is never a price. lite wrote 264 unpriced rows that way."""
    asked, handler = snapshot_sequence(
        [COLD_SKELETON], [COLD_SKELETON], [COLD_SKELETON], [PRICED_ROW]
    )
    md = client.marketdata
    with mocked() as mock:
        wire(mock, handler)
        rows = await md.raw_snapshot([265598], list(md.fields(underlying=False).values()))
    assert len(asked) == 4, f"the poll stopped on a priceless row after {len(asked)} calls"
    assert rows[265598]["84"] == "228.45"
    assert rows[265598]["7308"] == "0.51", "a field seen only on an early row was dropped"


async def test_a_conid_that_never_quotes_exhausts_the_budget_and_returns_what_it_had(
    client, shipped_settings, fake_clock
):
    """A closed venue serves a status and no price, and that is a real answer: no endless loop,
    and the status it did get is kept (a frozen quote is marked frozen from it)."""
    asked, handler = snapshot_sequence([COLD_SKELETON])
    marketdata = shipped_settings.ibkr.marketdata
    with mocked() as mock:
        wire(mock, handler)
        quotes = await client.marketdata.snapshot([265598])
        rows = await client.marketdata.raw_snapshot([265598], ["31", "84", "86", "6509"])
    assert len(asked) == 2 * marketdata.snapshot_settle_retries
    assert fake_clock.slept.count(marketdata.snapshot_settle_delay_seconds) == 2 * (
        marketdata.snapshot_settle_retries - 1
    )
    assert rows[265598]["6509"] == "ZBd"
    assert "31" not in rows[265598]
    quote = quotes[265598]
    assert quote.last is None
    assert quote.bid is None
    assert quote.ask is None
    assert quote.market_data_status is MarketDataStatus.FROZEN
    assert quote.delta == 0.51


async def test_a_row_that_quoted_is_not_clobbered_by_a_later_skeleton(client):
    """Every retry re-reads the whole batch, so a conid that already quoted would be overwritten
    while the loop waited on its neighbour."""
    asked, handler = snapshot_sequence(
        [{"conid": 1, "31": "10.0", "84": "9.9", "86": "10.1"}, {"conid": 2, "6509": "ZBd"}],
        [{"conid": 1, "6509": "ZBd"}, {"conid": 2, "31": "20.0", "84": "19.9", "86": "20.1"}],
    )
    with mocked() as mock:
        wire(mock, handler)
        quotes = await client.marketdata.snapshot([1, 2])
    assert len(asked) == 2
    assert quotes[1].last == 10.0, "the quote that arrived first was thrown away"
    assert quotes[2].last == 20.0


async def test_a_metadata_only_request_settles_on_the_first_response(client):
    """A status probe has no price to wait for, so it must not spend the settle budget."""
    asked, handler = snapshot_sequence([{"conid": 265598, "6509": "DPB"}])
    with mocked() as mock:
        wire(mock, handler)
        rows = await client.marketdata.raw_snapshot([265598], ["6509"])
    assert len(asked) == 1
    assert rows[265598]["6509"] == "DPB"


async def test_every_batch_is_primed_before_any_is_polled(make_session, shipped_settings):
    """A subscription starts warming when first asked for: measured in lite, a primed batch
    settled in 4.78 s against 7.03 s cold. The bare first response never settles, so the order
    cannot look primed by accident."""
    ibkr = with_section(shipped_settings.ibkr, "marketdata", max_conids_per_snapshot=2)
    md = marketdata_with(make_session, ibkr)
    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        conids = request.url.params["conids"]
        asked.append(conids)
        return httpx.Response(200, json=[{"conid": int(c)} for c in conids.split(",")])

    with mocked() as mock:
        wire(mock, handler)
        await md.raw_snapshot([1, 2, 3, 4], ["31", "84", "86"])
    assert asked[:2] == ["1,2", "3,4"], f"batch two was not primed first: {asked[:3]}"
    retries = ibkr.marketdata.snapshot_settle_retries
    assert len(asked) == 2 + 2 * retries


async def test_a_single_batch_is_not_primed_twice(client):
    """Its first poll IS the priming request."""
    asked, handler = snapshot_sequence([{"conid": 1, "31": "1.0", "84": "0.9", "86": "1.1"}])
    with mocked() as mock:
        wire(mock, handler)
        await client.marketdata.snapshot([1])
    assert len(asked) == 1


async def test_no_snapshot_request_carries_more_conids_than_the_cap(client, shipped_settings):
    """Measured in lite: 50 and 100 conids accepted, 150 refused with HTTP 429; the batching is
    asserted on the REQUESTS made, which no response fixture can express."""
    cap = shipped_settings.ibkr.marketdata.max_conids_per_snapshot
    sizes: list[int] = []
    seen: set[int] = set()

    def handler(request: httpx.Request) -> httpx.Response:
        conids = [int(c) for c in request.url.params["conids"].split(",")]
        sizes.append(len(conids))
        seen.update(conids)
        return httpx.Response(200, json=[{"conid": c, "31": "1.0"} for c in conids])

    wanted = list(range(1, cap * 2 + 8))
    with mocked() as mock:
        wire(mock, handler)
        rows = await client.marketdata.raw_snapshot(wanted, ["31"])
    assert sizes
    assert max(sizes) <= cap
    assert seen == set(wanted)
    assert set(rows) == set(wanted)


async def test_snapshot_batches_respect_a_configured_maximum(make_session, shipped_settings):
    ibkr = with_section(
        shipped_settings.ibkr,
        "marketdata",
        max_conids_per_snapshot=2,
        snapshot_settle_retries=1,
    )
    md = marketdata_with(make_session, ibkr)
    sizes: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        conids = request.url.params["conids"].split(",")
        sizes.append(len(conids))
        return httpx.Response(200, json=[{"conid": int(c), "31": "1.0"} for c in conids])

    with mocked() as mock:
        wire(mock, handler)
        await md.raw_snapshot([1, 2, 3, 4, 5], ["31"])
    assert max(sizes) <= 2


def test_the_configured_conid_cap_is_one_the_route_accepted(shipped_settings):
    """Pins the measurement: 100 was accepted live, 150 was not."""
    assert shipped_settings.ibkr.marketdata.max_conids_per_snapshot <= 100


async def test_more_fields_than_the_cap_are_refused_before_any_call(client, shipped_settings):
    cap = shipped_settings.ibkr.marketdata.max_fields_per_snapshot
    with mocked() as mock:
        route = mock.route().respond(200, json=[])
        with pytest.raises(ValueError, match="at most"):
            await client.marketdata.raw_snapshot([1], [str(i) for i in range(cap + 1)])
    assert route.call_count == 0


async def test_an_empty_conid_list_makes_no_call(client):
    with mocked() as mock:
        route = mock.route().respond(200, json=[])
        assert await client.marketdata.raw_snapshot([], ["31"]) == {}
        assert await client.marketdata.snapshot([]) == {}
    assert route.call_count == 0


async def test_snapshot_decoding_maps_field_ids_to_names(client):
    row = {
        "conid": 1,
        "31": "228.50",
        "84": "228.45",
        "86": "228.55",
        "87": "1,204",
        "7633": "31.42%",
        "7308": "0.5521",
        "6509": "RpB",
    }
    with mocked() as mock:
        mock.get(SNAPSHOT).respond(200, json=[row])
        quote = (await client.marketdata.snapshot([1]))[1]
    assert quote.last == 228.50
    assert quote.volume == 1204, "thousands separator cast"
    assert quote.implied_vol == pytest.approx(0.3142)
    assert quote.delta == 0.5521
    assert quote.market_data_status is MarketDataStatus.REALTIME
    assert quote.mid == pytest.approx(228.50)


async def test_the_underlying_snapshot_uses_the_underlying_field_ids(client):
    with mocked() as mock:
        route = mock.get(SNAPSHOT).respond(
            200, json=[{"conid": 1, "31": "10", "7283": "13.126%", "7087": "12%"}]
        )
        quote = (await client.marketdata.snapshot([1], underlying=True))[1]
    fields = route.calls.last.request.url.params["fields"].split(",")
    assert "7283" in fields
    assert "7633" not in fields
    assert quote.implied_vol == pytest.approx(0.13126)
    assert quote.historical_vol_30d == pytest.approx(0.12)


async def test_an_absent_quote_field_decodes_to_none_not_zero(client):
    """lite's original collapsed a missing bid to 0.0: an unquoted contract quoted wrongly."""
    with mocked() as mock:
        mock.get(SNAPSHOT).respond(200, json=[{"conid": 1, "31": "10.0"}])
        quote = (await client.marketdata.snapshot([1]))[1]
    assert quote.last == 10.0
    assert quote.bid is None
    assert quote.ask is None
    assert quote.mid is None


async def test_snapshot_rows_that_are_not_objects_or_lack_a_conid_are_ignored(client):
    with mocked() as mock:
        mock.get(SNAPSHOT).respond(200, json=["junk", {"31": "1.0"}, {"conid": 1, "31": "2.0"}])
        quotes = await client.marketdata.snapshot([1])
    assert list(quotes) == [1]
    assert quotes[1].last == 2.0


# ── /iserver/marketdata/history ──


async def test_history_passes_the_native_exchange_through(client):
    """Index contracts return empty on SMART; the native exchange is required."""
    body = {
        "symbol": "N225",
        "data": [{"t": 1755648000000, "o": 1.0, "h": 2.0, "l": 0.5, "c": 1.5, "v": 100}],
    }
    with mocked() as mock:
        route = mock.get("iserver/marketdata/history").respond(200, json=body)
        history = await client.history.bars(416843, period="5y", bar="1w", exchange="OSE.JPN")
    params = dict(route.calls.last.request.url.params)
    assert params["exchange"] == "OSE.JPN"
    assert params["period"] == "5y"
    assert params["bar"] == "1w"
    assert len(history.bars) == 1
    assert history.bars[0].close == 1.5


async def test_an_omitted_exchange_is_absent_from_the_wire_not_the_string_none(client):
    """Measured in lite: SAN and TTE answer HTTP 500 "Chart data unavailable" with their listing
    venue and serve daily bars with the exchange omitted; ``exchange=None`` on the query string
    would be a third, wrong request."""
    with mocked() as mock:
        route = mock.get("iserver/marketdata/history").respond(
            200, json={"symbol": "SAN", "data": []}
        )
        await client.history.bars(30314144, period="1m", bar="1d", exchange=None)
    params = dict(route.calls.last.request.url.params)
    assert "exchange" not in params
    assert "None" not in params.values()
    assert params["conid"] == "30314144"


# ── options chains ──


async def test_option_strikes_are_cast_from_strings(client):
    """Strikes come back as strings; cast before arithmetic."""
    with mocked() as mock:
        route = mock.get("iserver/secdef/strikes").respond(
            200, json={"call": ["95", "100", "105.5"], "put": ["95"]}
        )
        strikes = await client.secdef.strikes(8314, month="JAN26", exchange="SMART")
    assert strikes.call == (95.0, 100.0, 105.5)
    assert all(isinstance(value, float) for value in strikes.call)
    assert strikes.put == (95.0,)
    assert dict(route.calls.last.request.url.params) == {
        "conid": "8314",
        "sectype": "OPT",
        "month": "JAN26",
        "exchange": "SMART",
    }


async def test_unparseable_strikes_are_dropped_rather_than_zeroed(client):
    with mocked() as mock:
        mock.get("iserver/secdef/strikes").respond(
            200, json={"call": ["95", None, "n/a"], "put": []}
        )
        strikes = await client.secdef.strikes(8314, month="JAN26", exchange="SMART")
    assert strikes.call == (95.0,)
    assert strikes.put == ()


async def test_option_contracts_rejects_an_invalid_right_before_any_call(client):
    with mocked() as mock:
        route = mock.route().respond(200, json=[])
        with pytest.raises(ValueError, match="right must be"):
            await client.secdef.option_contracts(
                8314, month="JAN26", exchange="SMART", strike=100, right="X"
            )
    assert route.call_count == 0


async def test_option_contracts_send_the_upper_case_right_and_keep_rows_with_a_conid(client):
    rows = [
        {"conid": 501, "symbol": "IBM", "maturityDate": "20260116", "underlying_con_id": 8314},
        {"symbol": "no conid"},
        {"conid": -1},
        "junk",
    ]
    with mocked() as mock:
        route = mock.get("iserver/secdef/info").respond(200, json=rows)
        contracts = await client.secdef.option_contracts(
            8314, month="JAN26", exchange="SMART", strike=100.0, right="c"
        )
    assert route.calls.last.request.url.params["right"] == "C"
    assert [contract.conid for contract in contracts] == [501]
    assert contracts[0].underlying_conid == 8314
    assert contracts[0].maturity_date == "20260116"


# ── futures (data only) ──


async def test_the_futures_ladder_asks_every_root_in_one_call_sorted_by_expiry(client):
    body = {
        "CL": [
            {"conid": 3, "symbol": "CLJ6", "expirationDate": "20260420"},
            {"conid": 1, "symbol": "CLG6", "expirationDate": "20260220"},
            {"symbol": "CLH6", "expirationDate": "20260320"},
            {"conid": 2, "symbol": "CLK6"},
        ],
        "NG": [{"conid": 9, "symbol": "NGG6", "expirationDate": "20260226"}],
    }
    with mocked() as mock:
        route = mock.get("trsrv/futures").respond(200, json=body)
        ladder = await client.secdef.futures(["CL", "NG"])
    assert route.call_count == 1
    assert route.calls.last.request.url.params["symbols"] == "CL,NG"
    assert [rung.conid for rung in ladder["CL"]] == [1, 3], "missing conid or expiry dropped"
    assert [rung.expiry for rung in ladder["CL"]] == [date(2026, 2, 20), date(2026, 4, 20)]
    assert [rung.conid for rung in ladder["NG"]] == [9]


async def test_an_empty_futures_root_list_makes_no_call(client):
    with mocked() as mock:
        route = mock.route().respond(200, json={})
        assert await client.secdef.futures([]) == {}
    assert route.call_count == 0


# ── /portfolio ──


async def test_positions_page_until_an_empty_page(client):
    pages: dict[int, list[dict[str, Any]]] = {
        0: [{"conid": 1, "position": 10}, {"conid": 2, "position": "5"}],
        1: [{"conid": 3, "position": 1.5, "mktPrice": 0.1, "currency": "usd"}],
        2: [],
    }
    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(rel(request))
        return httpx.Response(200, json=pages.get(int(rel(request).rsplit("/", 1)[-1]), []))

    with mocked() as mock:
        wire(mock, handler)
        positions = await client.portfolio.positions()
    assert [position.conid for position in positions] == [1, 2, 3]
    assert positions[1].quantity == Decimal("5")
    assert positions[2].quantity == Decimal("1.5")
    assert positions[2].market_price == Decimal("0.1")
    assert positions[2].currency == "USD"
    assert asked == [f"portfolio/{ACCOUNT}/positions/{page}" for page in range(3)]


async def test_positions_stop_at_max_position_pages(make_session, shipped_settings, guard):
    pages = 3
    ibkr = with_section(shipped_settings.ibkr, "portfolio", max_position_pages=pages)
    client = IbkrClient(
        make_session(ibkr=ibkr), shipped_settings.model_copy(update={"ibkr": ibkr}), guard
    )
    with mocked() as mock:
        route = mock.route().respond(200, json=[{"conid": 1, "position": 1}])
        positions = await client.portfolio.positions()
    assert route.call_count == pages
    assert len(positions) == pages


async def test_position_rows_without_a_conid_or_quantity_are_skipped(client):
    rows = [{"conid": 1}, {"position": 2}, {"conid": 4, "position": "3"}, "junk"]
    with mocked() as mock:
        mock.get(f"portfolio/{ACCOUNT}/positions/0").respond(200, json=rows)
        positions = await client.portfolio.positions_page(0)
    assert [(p.conid, p.quantity) for p in positions] == [(4, Decimal("3"))]


async def test_accounts_are_cached_and_refreshed_on_request(client):
    with mocked() as mock:
        route = mock.get("portfolio/accounts").respond(
            200, json=[{"id": ACCOUNT, "currency": "EUR", "type": "DEMO"}, {"type": "no id"}]
        )
        first = await client.portfolio.accounts()
        second = await client.portfolio.accounts()
        assert route.call_count == 1, "IBKR allows one call per five seconds"
        await client.portfolio.accounts(refresh=True)
        assert route.call_count == 2
    assert first is second
    assert [account.account_id for account in first] == [ACCOUNT]
    assert first[0].currency == "EUR"
    assert first[0].account_type == "DEMO"
    assert ACCOUNT not in repr(first[0])


def ledger_body(base: str, other: str) -> dict[str, Any]:
    """A ledger whose BASE row names itself BASE, as measured [CP ✓ 2026-09-10]."""
    return {
        "BASE": {
            "netliquidationvalue": "125000.50",
            "cashbalance": 9000,
            "currency": "BASE",
            "secondkey": "BASE",
        },
        base: {"cashbalance": 1000.5, "settledcash": "1,000.50", "exchangerate": 1.0},
        other: {"cashbalance": "-0", "exchangerate": 0.92},
    }


async def test_ledger_nav_and_cash_via_the_client(client, shipped_settings):
    base = shipped_settings.account.base_currency
    other = "USD" if base != "USD" else "EUR"
    with mocked() as mock:
        route = mock.get(f"portfolio/{ACCOUNT}/ledger").respond(200, json=ledger_body(base, other))
        ledger = await client.portfolio.ledger()
        nav = await client.portfolio.nav()
        cash = await client.portfolio.cash_balances()
    assert route.call_count == 3
    assert ledger.nav is not None
    assert ledger.nav.nav == Decimal("125000.50")
    assert ledger.nav.currency == base
    assert ledger.base_currency == base
    assert nav == ledger.nav
    assert {balance.currency: balance.cash for balance in cash} == {
        base: Decimal("1000.5"),
        other: Decimal("0"),
    }
    assert next(b for b in cash if b.currency == base).settled_cash == Decimal("1000.50")


async def test_a_ledger_in_another_base_currency_has_no_nav(client, shipped_settings):
    """A NAV in another currency is refused, never converted."""
    base = shipped_settings.account.base_currency
    foreign = "JPY" if base != "JPY" else "CHF"
    with mocked() as mock:
        mock.get(f"portfolio/{ACCOUNT}/ledger").respond(200, json=ledger_body(foreign, base))
        ledger = await client.portfolio.ledger()
    assert ledger.nav is None
    assert ledger.base_currency == foreign
    assert ledger.nav_refusal is not None
    assert "refused" in ledger.nav_refusal


async def test_the_client_starts_as_a_context_manager_and_runs_the_account_guard(
    client, fake_transport
):
    with mocked() as mock:
        mock.post("iserver/auth/ssodh/init").respond(200, json={"authenticated": True})
        mock.get("iserver/accounts").respond(200, json={"accounts": [ACCOUNT]})
        mock.get("portfolio/accounts").respond(200, json=[{"id": ACCOUNT}])
        async with client as started:
            assert started is client
    assert fake_transport.prepared == 1
    assert fake_transport.closed == 1


async def test_an_account_this_login_does_not_list_refuses_to_start(client):
    """lite raised on an empty account list; the guard now refuses any login without the
    configured account."""
    with mocked() as mock:
        mock.post("iserver/auth/ssodh/init").respond(200, json={"authenticated": True})
        mock.get("iserver/accounts").respond(200, json={"accounts": []})
        mock.get("portfolio/accounts").respond(200, json=[])
        with pytest.raises(AccountGuardError, match="Nothing starts") as caught:
            await client.start()
    assert ACCOUNT not in str(caught.value)


# ── contracts: rules, algos, trading schedule ──


async def test_rules_post_conid_side_and_exchange_and_decode_the_answer(client):
    body = {
        "rules": {
            "orderTypes": ["limit", "stop_limit", None],
            "orderTypesOutside": ["limit"],
            "cqtTypes": ["limit"],
            "tifTypes": ["DAY", "GTC"],
            "algoEligible": True,
            "sizeIncrement": 1,
            "incrementRules": [
                {"lowerEdge": 0.0, "increment": 0.01},
                {"lowerEdge": 1.0, "increment": 0},
                "junk",
            ],
            "priceMagnifier": 1,
            "cashCcy": "usd",
        }
    }
    with mocked() as mock:
        route = mock.post("iserver/contract/rules").respond(200, json=body)
        rules = await client.contracts.rules(265598, exchange="SMART", is_buy=False)
    assert json.loads(route.calls.last.request.content) == {
        "conid": 265598,
        "isBuy": False,
        "exchange": "SMART",
    }
    assert rules.conid == 265598
    assert rules.is_buy is False
    assert rules.order_types == ("limit", "stop_limit")
    assert rules.order_types_outside_rth == ("limit",)
    assert rules.cash_quantity_order_types == ("limit",)
    assert rules.tif_types == ("DAY", "GTC")
    assert rules.algo_eligible is True
    assert rules.size_increment == 1.0
    assert [(r.lower_edge, r.increment) for r in rules.increment_rules] == [(0.0, 0.01)]
    assert rules.cash_currency == "USD"
    assert rules.error is None


async def test_rules_without_an_exchange_leave_it_off_the_body(client):
    with mocked() as mock:
        route = mock.post("iserver/contract/rules").respond(200, json={"orderTypes": ["limit"]})
        rules = await client.contracts.rules(265598)
    assert json.loads(route.calls.last.request.content) == {"conid": 265598, "isBuy": True}
    assert rules.order_types == ("limit",)
    assert rules.algo_eligible is None


async def test_algos_ask_for_descriptions_and_parameters_and_decode_them(client):
    body = {
        "algos": [
            {
                "id": "Adaptive",
                "name": "Adaptive",
                "parameters": [
                    {
                        "id": "adaptivePriority",
                        "name": "Priority",
                        "valueClassName": "String",
                        "required": "true",
                        "legalStrings": ["Urgent", "Normal", "Patient"],
                    },
                    {"name": "no id"},
                ],
            },
            {"name": "no id"},
        ]
    }
    with mocked() as mock:
        route = mock.get("iserver/contract/265598/algos").respond(200, json=body)
        algos = await client.contracts.algos(265598)
    assert dict(route.calls.last.request.url.params) == {"addDescription": "1", "addParams": "1"}
    [algo] = algos
    assert algo.algo_id == "Adaptive"
    [parameter] = algo.parameters
    assert parameter.parameter_id == "adaptivePriority"
    assert parameter.required is True
    assert parameter.legal_values == ("Urgent", "Normal", "Patient")


async def test_the_trading_schedule_decodes_liquid_hours_per_day(client):
    def epoch(day: int, hour: int, minute: int = 0) -> int:
        return int(datetime(2026, 10, day, hour, minute, tzinfo=UTC).timestamp())

    body = {
        "exchange_time_zone": "America/New_York",
        "schedules": {
            "20261013": {
                "liquid_hours": [{"opening": epoch(13, 13, 30), "closing": epoch(13, 20)}]
            },
            "20261012": {
                "liquid_hours": [
                    {"opening": epoch(12, 13, 30), "closing": epoch(12, 20)},
                    {"opening": epoch(12, 20), "closing": epoch(12, 19)},
                ]
            },
            "bogus": {"liquid_hours": []},
        },
    }
    with mocked() as mock:
        route = mock.get("contract/trading-schedule").respond(200, json=body)
        schedule = await client.contracts.trading_schedule(265598, exchange="NASDAQ")
        await client.contracts.trading_schedule(265598)
    assert dict(route.calls[0].request.url.params) == {"conid": "265598", "exchange": "NASDAQ"}
    assert dict(route.calls[1].request.url.params) == {"conid": "265598"}
    assert schedule.timezone == "America/New_York"
    assert [session.day for session in schedule.sessions] == [
        date(2026, 10, 12),
        date(2026, 10, 13),
    ]
    assert schedule.sessions[0].open == datetime(2026, 10, 12, 13, 30, tzinfo=UTC)
    assert schedule.first_day == date(2026, 10, 12)
    assert schedule.last_day == date(2026, 10, 13)


# ── the scanner ──


async def test_scanner_params_decode(client):
    body = {
        "scan_type_list": [
            {"code": "TOP_PERC_GAIN", "display_name": "Top % Gainers", "instruments": ["STK"]},
            {"display_name": "no code"},
        ],
        "instrument_list": [{"type": "STK"}, {"display_name": "no type"}],
        "filter_list": [{"code": "priceAbove"}],
        "location_tree": [{"locations": [{"type": "STK.US.MAJOR"}, {"type": "STK.US"}]}],
    }
    with mocked() as mock:
        route = mock.get("iserver/scanner/params").respond(200, json=body)
        params = await client.scanner.params()
    assert route.calls.last.request.method == "GET"
    assert [scan.code for scan in params.scan_types] == ["TOP_PERC_GAIN"]
    assert params.scan_types[0].instruments == ("STK",)
    assert params.instruments == ("STK",)
    assert params.filters == ("priceAbove",)
    assert params.locations == ("STK.US.MAJOR", "STK.US")


async def test_scanner_run_posts_its_body_and_decodes_contracts(client):
    body = {
        "contracts": [
            {"con_id": 265598, "symbol": "AAPL", "listing_exchange": "NASDAQ", "sec_type": "STK"},
            {"symbol": "no conid"},
        ]
    }
    with mocked() as mock:
        route = mock.post("iserver/scanner/run").respond(200, json=body)
        contracts = await client.scanner.run(
            instrument="STK",
            scan_type="TOP_PERC_GAIN",
            location="STK.US.MAJOR",
            filters=[{"code": "priceAbove", "value": 5}],
        )
    assert json.loads(route.calls.last.request.content) == {
        "instrument": "STK",
        "type": "TOP_PERC_GAIN",
        "location": "STK.US.MAJOR",
        "filter": [{"code": "priceAbove", "value": 5}],
    }
    assert [(c.conid, c.symbol, c.listing_exchange) for c in contracts] == [
        (265598, "AAPL", "NASDAQ")
    ]


# ── what the package must never contain ──


def _ibkr_sources() -> list[Path]:
    return sorted(IBKR_PACKAGE.rglob("*.py"))


def _executable_source(path: Path) -> str:
    """Module source with docstrings and comments stripped: these tests are about the code, and
    the docstrings deliberately name what was rejected."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                body.pop(0)
    return ast.unparse(tree)


def test_the_ibkr_package_is_found():
    assert len(_ibkr_sources()) > 5


def test_tls_verification_is_never_disabled_in_the_ibkr_package():
    """IBKR-Portmanteau fell back to verify=False whenever its CA file was absent."""
    for path in _ibkr_sources():
        code = _executable_source(path)
        assert "verify=False" not in code, path
        assert "CERT_NONE" not in code, path


def test_no_http_client_is_built_with_verification_off():
    for path in _ibkr_sources():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if keyword.arg == "verify":
                    assert not (
                        isinstance(keyword.value, ast.Constant) and keyword.value.value is False
                    ), f"{path}:{node.lineno}"


def test_no_tls_bypass_reaches_the_session_or_client_constructor():
    for constructor in (BrokerSession.__init__, IbkrClient.__init__):
        names = inspect.signature(constructor).parameters
        assert not any("verify" in name or "insecure" in name for name in names)


def test_the_arbitrary_endpoint_passthrough_was_not_ported(client):
    """lite's query_ibkr_endpoint guarded writes with a substring blocklist, which fails open:
    /iserver/reply/{id} confirms an order and contains none of the blocked words."""
    assert not hasattr(IbkrClient, "query_ibkr_endpoint")
    for group in (
        client,
        client.portfolio,
        client.marketdata,
        client.history,
        client.secdef,
        client.contracts,
        client.scanner,
    ):
        for name, member in inspect.getmembers(group, inspect.ismethod):
            if name.startswith("_"):
                continue
            assert "path" not in inspect.signature(member).parameters, f"{type(group)}.{name}"
    for path in _ibkr_sources():
        code = _executable_source(path)
        assert "query_ibkr_endpoint" not in code
        assert "dangerous_patterns" not in code


def test_no_yahoo_binance_or_coingecko_dependency():
    """Out of scope, and unusable unattended: Yahoo's chain endpoint is crumb-gated."""
    for path in _ibkr_sources():
        code = _executable_source(path).lower()
        for host in ("yahoo", "binance", "coingecko", "crumb"):
            assert host not in code, path
