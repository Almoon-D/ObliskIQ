"""The Client Portal endpoint layer, against recorded fixtures.

No live IBKR access in this sandbox, so every response here is a fixture shaped
like the documented one. The behaviour being pinned is ours — in particular the
exact-ticker-match rule, which is the single most valuable thing carried over
from IBKR-Portmanteau.

Everything about the wire format stays UNVERIFIED until Session 5.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import json

import httpx
import pytest

from conftest import without_backoff
from obliskiq.errors import AuthError, PermanentUpstreamError, UpstreamError
from obliskiq.ibkr.cp_client import SOURCE, CpApiClient


class StubOAuth:
    """Stands in for a live-session-token-bearing OAuthSession."""

    def sign_request(self, method, url, *, params=None):
        return {"Authorization": 'OAuth realm="limited_poa"', "Accept": "application/json"}


def make_client(config, handler, **kwargs) -> CpApiClient:
    transport = httpx.MockTransport(handler)
    return CpApiClient(
        "https://api.ibkr.com",
        StubOAuth(),
        without_backoff(config),
        client=httpx.AsyncClient(transport=transport),
        **kwargs,
    )


def json_route(routes: dict[str, object], *, default_status: int = 404):
    """Dispatch on path, returning the mapped JSON body."""

    def handler(request: httpx.Request) -> httpx.Response:
        for path, body in routes.items():
            if request.url.path.endswith(path):
                if isinstance(body, tuple):
                    status, payload = body
                    return httpx.Response(status, json=payload)
                return httpx.Response(200, json=body)
        return httpx.Response(default_status, json={"error": "no route"})

    return handler


# ── /iserver/secdef/search and the exact-match rule ──

# The API ranks by its own relevance. Here the wanted ticker is NOT first.
SEARCH_BP = [
    {"conid": 111, "symbol": "BPAQF", "companyName": "BP p.l.c. - unsponsored ADR"},
    {"conid": 222, "symbol": "BP", "companyName": "BP p.l.c."},
    {"conid": 333, "symbol": "BPT", "companyName": "BP Prudhoe Bay Royalty Trust"},
]


@pytest.fixture()
def search_client(config):
    return make_client(config, json_route({"/iserver/secdef/search": SEARCH_BP}))


async def test_exact_ticker_match_beats_the_apis_own_ordering(search_client):
    """The ported rule, and the reason resolve_conid exists rather than a bare
    search call: secdef/search ranks by relevance, which is not correctness."""
    assert await search_client.resolve_conid("BP") == 222
    await search_client.aclose()


async def test_exact_match_is_case_insensitive(search_client):
    assert await search_client.resolve_conid("bp") == 222
    await search_client.aclose()


async def test_falls_back_to_the_first_row_when_nothing_matches_exactly(search_client):
    """Configured by ibkr.cp_api.secdef.accept_first_on_no_exact_match."""
    assert await search_client.resolve_conid("BPX") == 111
    await search_client.aclose()


async def test_the_fallback_can_be_switched_off(config):
    import copy

    data = copy.deepcopy(config.as_dict())
    data["ibkr"]["cp_api"]["secdef"]["accept_first_on_no_exact_match"] = False
    from obliskiq.config import Config

    client = make_client(Config(data), json_route({"/iserver/secdef/search": SEARCH_BP}))
    assert await client.resolve_conid("BPX") is None
    await client.aclose()


# The measured shape of a dual-listed search: EVERY row answers the bare ticker,
# so the exact-match rule matches all of them and returns the API's first choice.
# The venue is in `description`. Ordering and venues are ASML's, [CP 2026-09-10].
SEARCH_ASML = [
    {"conid": 117902840, "symbol": "ASML", "description": "NASDAQ"},
    {"conid": 762277717, "symbol": "ASML", "description": "TSE"},
    {"conid": 117589399, "symbol": "ASML", "description": "AEB"},
]


@pytest.fixture()
def dual_listed_client(config):
    return make_client(config, json_route({"/iserver/secdef/search": SEARCH_ASML}))


async def test_the_exact_ticker_rule_cannot_separate_one_companys_listings(dual_listed_client):
    """The half that was missing, stated as the failure it caused.

    Every listing answers "ASML", so the exact-match rule matches the NASDAQ row
    first and returns it — under a universe row declaring AEB and EUR. That is
    how New York's chain was collected against an Amsterdam listing for a whole
    session, and no amount of exact-ticker matching would ever have caught it.
    """
    assert await dual_listed_client.resolve_conid("ASML") == 117902840
    await dual_listed_client.aclose()


async def test_the_declared_venue_picks_the_listing_that_was_declared(dual_listed_client):
    """Verified live [CP 2026-09-10] on all three European rows: ASML/AEB, SAN/BM
    and TTE/SBF each resolve to the European conid with the venue and to the US
    one without it."""
    assert await dual_listed_client.resolve_conid("ASML", exchange="AEB") == 117589399
    await dual_listed_client.aclose()


async def test_the_declared_venue_is_matched_case_insensitively(dual_listed_client):
    assert await dual_listed_client.resolve_conid("ASML", exchange="aeb") == 117589399
    await dual_listed_client.aclose()


async def test_a_declared_venue_that_matches_nothing_falls_through(dual_listed_client):
    """A TIEBREAK, not a filter. It can only ever sharpen a resolution — a venue
    the search never mentions must never turn a working row into a failure."""
    assert await dual_listed_client.resolve_conid("ASML", exchange="NOSUCH") == 117902840
    await dual_listed_client.aclose()


async def test_the_venue_never_overrides_the_ticker_rule(config):
    """A row on the right exchange under the WRONG ticker is a different company."""
    body = [
        {"conid": 1, "symbol": "OTHER", "description": "AEB"},
        {"conid": 2, "symbol": "ASML", "description": "NASDAQ"},
    ]
    client = make_client(config, json_route({"/iserver/secdef/search": body}))

    assert await client.resolve_conid("ASML", exchange="AEB") == 2
    await client.aclose()


async def test_no_declared_venue_leaves_the_old_behaviour_exactly_as_it_was(search_client):
    """Every row without an `exchange:` declaration keeps the resolution it had."""
    assert await search_client.resolve_conid("BP") == 222
    assert await search_client.resolve_conid("BPX") == 111
    await search_client.aclose()


async def test_no_results_resolves_to_none(config):
    client = make_client(config, json_route({"/iserver/secdef/search": []}))
    assert await client.resolve_conid("NOSUCHTHING") is None
    await client.aclose()


async def test_rows_without_a_conid_are_skipped(config):
    client = make_client(
        config,
        json_route({"/iserver/secdef/search": [{"symbol": "X"}, {"conid": 5, "symbol": "X"}]}),
    )
    assert await client.resolve_conid("X") == 5
    await client.aclose()


async def test_search_exposes_option_metadata(config):
    rows = [
        {
            "conid": 8314,
            "symbol": "IBM",
            "companyName": "INTL BUSINESS MACHINES CORP",
            "sections": [
                {"secType": "STK"},
                {"secType": "OPT", "months": "JAN26;FEB26;MAR26", "exchange": "SMART,CBOE"},
            ],
        }
    ]
    client = make_client(config, json_route({"/iserver/secdef/search": rows}))
    matches = await client.search_contracts("IBM")
    assert matches[0].has_options
    assert matches[0].option_months() == ("JAN26", "FEB26", "MAR26")
    assert matches[0].option_exchanges() == ("SMART", "CBOE")
    await client.aclose()


async def test_search_sends_its_terms_as_a_json_body_not_a_query_string(config):
    """The encoding is the whole finding, and nothing pinned it before.

    Sent as query parameters the endpoint still answers HTTP 200 and still
    matches the symbol — it just returns every row with ``sections: []``, which
    is indistinguishable from an instrument with no listed options. That is what
    made the first live run report "no option months" for all 24 chain targets.
    Measured on SPY, 2026-09-10: query parameters gave 55 matches and 0 sections;
    the same search as a JSON body gave 3 matches and 6 sections.
    """
    seen = {}

    def handler(request):
        seen["params"] = dict(request.url.params)
        seen["body"] = json.loads(request.content) if request.content else None
        return httpx.Response(200, json=[])

    client = make_client(config, handler)
    await client.search_contracts("SPY")
    assert seen["body"] == {"symbol": "SPY", "secType": "STK", "name": False}
    assert seen["params"] == {}, "search terms in the query string come back sectionless"
    await client.aclose()


# ── /iserver/marketdata/snapshot ──


#: What a cold subscription ACTUALLY returns before it quotes, measured
#: 2026-09-15 on cp_api: the market-data status and all four Greeks arrive
#: together about 1.3s in, and no price for another five seconds. The skeleton
#: this file used to use — `{"conid": ..., "_updated": 1}` — carried none of the
#: requested fields, so it exercised a poll that the real API never triggers, and
#: the tests passed while the collector wrote a whole phase of unpriced rows.
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


async def test_snapshot_polls_until_the_fields_settle(config):
    """The first call for an unsubscribed conid returns a skeleton and the fields
    populate later. The original returned the skeleton as if it were a quote."""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, json=[COLD_SKELETON])
        return httpx.Response(200, json=[PRICED_ROW])

    client = make_client(config, handler)
    snap = await client.market_snapshot([265598])
    assert calls["n"] >= 2
    assert snap[265598]["31"] == "228.50"
    await client.aclose()


async def test_a_status_and_greeks_skeleton_does_not_end_the_poll(config):
    """The defect that cost Phase 8 its first pass over every new option conid.

    The poll used to break as soon as ANY requested field appeared, and what
    appears first on a cold subscription is never a price — `6509` and the four
    Greeks arrive together about five seconds before bid and ask. So the collector
    took a skeleton for a quote and wrote 264 rows with no price on them.

    Excluding only `6509` would not have fixed it either: delta alone still ends
    the poll. Settlement is defined by the PRICE fields arriving.
    """
    seen = []

    def handler(request):
        seen.append(1)
        # Three cold responses carrying status and Greeks, then the prices.
        if len(seen) <= 3:
            return httpx.Response(200, json=[COLD_SKELETON])
        return httpx.Response(200, json=[PRICED_ROW])

    client = make_client(config, handler)
    snap = await client.market_snapshot([265598])
    assert len(seen) == 4, f"the poll stopped on a priceless row after {len(seen)} calls"
    assert snap[265598]["84"] == "228.45"
    assert snap[265598]["7308"] == "0.51", "a field seen only on an early row was dropped"
    await client.aclose()


async def test_a_conid_that_never_quotes_exhausts_the_budget_and_returns_what_it_had(config):
    """A closed venue serves a status and no price, and that is a real answer.

    It must not loop forever, and it must not throw away the status it did get —
    the grid reads market_data_status to mark a frozen quote as frozen.
    """
    seen = []

    def handler(request):
        seen.append(1)
        return httpx.Response(200, json=[COLD_SKELETON])

    client = make_client(config, handler)
    snap = await client.market_snapshot([265598])
    assert len(seen) == config.require_int("ibkr.cp_api.marketdata.snapshot_settle_retries")
    assert snap[265598]["6509"] == "ZBd"
    assert "31" not in snap[265598]
    await client.aclose()


async def test_a_row_that_quoted_early_is_not_clobbered_by_a_later_skeleton(config):
    """Every retry re-reads the WHOLE batch, so assigning the row outright let a
    conid that had already quoted be overwritten while the loop waited on its
    neighbour."""
    seen = []

    def handler(request):
        seen.append(1)
        if len(seen) == 1:
            # 1 quotes immediately; 2 is still cold.
            return httpx.Response(
                200,
                json=[
                    {"conid": 1, "31": "10.0", "84": "9.9", "86": "10.1"},
                    {"conid": 2, "6509": "ZBd"},
                ],
            )
        # 1 reverts to a skeleton while 2 finally quotes.
        return httpx.Response(
            200,
            json=[
                {"conid": 1, "6509": "ZBd"},
                {"conid": 2, "31": "20.0", "84": "19.9", "86": "20.1"},
            ],
        )

    client = make_client(config, handler)
    snap = await client.market_snapshot([1, 2])
    assert snap[1]["31"] == "10.0", "the quote that arrived first was thrown away"
    assert snap[2]["31"] == "20.0"
    await client.aclose()


async def test_a_metadata_only_request_settles_on_the_first_response(config):
    """A status probe has no price to wait for, so it must not spend the budget.

    The settle rule is 'a price arrived'; a caller that asked for no price field
    would otherwise poll its whole budget every time and learn nothing.
    """
    seen = []

    def handler(request):
        seen.append(1)
        return httpx.Response(200, json=[{"conid": 265598, "6509": "DPB"}])

    client = make_client(config, handler)
    snap = await client.market_snapshot([265598], fields=["6509"])
    assert len(seen) == 1
    assert snap[265598]["6509"] == "DPB"
    await client.aclose()


async def test_every_batch_is_primed_before_any_of_them_is_polled(config):
    """A subscription starts warming when it is first asked for.

    Polling batch one to completion before touching batch two serialised the
    warm-ups; measured 2026-09-15, a primed batch settled in 4.78s against 7.03s
    cold. So the first pass touches every batch before the second pass waits on
    any of them.
    """
    import copy

    from obliskiq.config import Config

    data = copy.deepcopy(config.as_dict())
    data["ibkr"]["cp_api"]["marketdata"]["max_conids_per_snapshot"] = 2

    order = []

    def handler(request):
        conids = request.url.params["conids"]
        order.append(conids)
        # The bare first response the route really serves, so neither the old
        # settle rule nor the new one ends the poll early. With a 6509 skeleton
        # here the OLD code settles immediately and the call order comes out
        # primed-looking by accident, which would make this test prove nothing.
        return httpx.Response(200, json=[{"conid": int(c)} for c in conids.split(",")])

    client = make_client(Config(data), handler)
    await client.market_snapshot([1, 2, 3, 4])
    assert order[:2] == ["1,2", "3,4"], (
        f"batch two was not primed before batch one was polled: {order[:3]}"
    )
    await client.aclose()


async def test_a_single_batch_is_not_primed_twice(config):
    """Its first poll IS the priming request; a second would spend a call to learn
    nothing."""
    seen = []

    def handler(request):
        seen.append(1)
        return httpx.Response(200, json=[{"conid": 1, "31": "1.0", "84": "0.9", "86": "1.1"}])

    client = make_client(config, handler)
    await client.market_snapshot([1])
    assert len(seen) == 1
    await client.aclose()


async def test_snapshot_decoding_maps_field_ids_to_names(config):
    row = {
        "conid": 1,
        "31": "228.50",
        "84": "228.45",
        "86": "228.55",
        "87": "1,204",
        "7283": "0.3142",
        "7308": "0.5521",
    }
    client = make_client(config, json_route({"/snapshot": [row]}))
    decoded = client.decode_snapshot(row)
    assert decoded["last"] == 228.50
    assert decoded["volume"] == 1204  # thousands separator cast, per the playbook
    assert decoded["implied_vol"] == 0.3142
    await client.aclose()


async def test_an_absent_quote_field_decodes_to_none_not_zero(config):
    """A missing bid is not a bid of zero. The original collapsed both to 0.0, which
    silently turns an unquoted contract into a quoted one at the wrong price."""
    client = make_client(config, json_route({"/snapshot": []}))
    decoded = client.decode_snapshot({"conid": 1, "31": "10.0"})
    assert decoded["last"] == 10.0
    assert decoded["bid"] is None and decoded["ask"] is None
    await client.aclose()


async def test_snapshot_batches_respect_the_configured_maximum(config):
    import copy

    from obliskiq.config import Config

    data = copy.deepcopy(config.as_dict())
    data["ibkr"]["cp_api"]["marketdata"]["max_conids_per_snapshot"] = 2
    data["ibkr"]["cp_api"]["marketdata"]["snapshot_settle_retries"] = 1

    seen = []

    def handler(request):
        conids = request.url.params["conids"].split(",")
        seen.append(len(conids))
        return httpx.Response(200, json=[{"conid": int(c), "31": "1.0"} for c in conids])

    client = make_client(Config(data), handler)
    await client.market_snapshot([1, 2, 3, 4, 5])
    assert max(seen) <= 2
    await client.aclose()


async def test_no_snapshot_request_carries_more_conids_than_the_cap(config):
    """The cap is the ROUTE's, and exceeding it costs the whole target.

    Measured 2026-09-10 by walking one request up on QQQ option conids: 50 ok,
    100 ok, 150 / 200 / 233 each refused with HTTP 429
    ``{"error":"Exceed number of conids"}``. The batching loop is what keeps every
    request under it, so this asserts on the REQUESTS the client actually made
    rather than on a recorded response — a fixture cannot express "and it was
    split in two".
    """
    sizes = []

    def handler(request):
        conids = dict(request.url.params).get("conids", "")
        sizes.append(len(conids.split(",")) if conids else 0)
        return httpx.Response(
            200, json=[{"conid": int(c), "31": "1.0"} for c in conids.split(",") if c]
        )

    cap = config.require_int("ibkr.cp_api.marketdata.max_conids_per_snapshot")
    client = make_client(config, handler)
    await client.market_snapshot(list(range(1, cap * 2 + 8)), fields=["31"])

    assert sizes, "the snapshot must actually have been requested"
    assert max(sizes) <= cap
    await client.aclose()


async def test_the_configured_cap_is_one_the_route_accepted(config):
    """Pins the measurement, so raising it back is a decision somebody makes on
    purpose. 100 was accepted live; 150 was not."""
    assert config.require_int("ibkr.cp_api.marketdata.max_conids_per_snapshot") <= 100


async def test_empty_conid_list_short_circuits(config):
    client = make_client(config, json_route({}))
    assert await client.market_snapshot([]) == {}
    await client.aclose()


# ── /iserver/marketdata/history (written new) ──


async def test_market_history_passes_the_native_exchange_through(config):
    """Index contracts return empty on SMART; the native exchange is required."""
    captured = {}

    def handler(request):
        captured.update(dict(request.url.params))
        return httpx.Response(
            200,
            json={
                "symbol": "N225",
                "data": [{"t": 1755648000000, "o": 1.0, "h": 2.0, "l": 0.5, "c": 1.5, "v": 100}],
            },
        )

    client = make_client(config, handler)
    body = await client.market_history(416843, period="5y", bar="1w", exchange="OSE.JPN")
    assert captured["exchange"] == "OSE.JPN"
    assert captured["period"] == "5y" and captured["bar"] == "1w"
    assert len(body["data"]) == 1
    await client.aclose()


async def test_a_search_that_found_nothing_is_no_matches_and_not_a_crash(config):
    """The route answers a LIST on a hit and an OBJECT on a miss, both HTTP 200.

    Measured on TOPIX [CP 2026-09-10]: ``{"error": "No symbol found"}``. Iterating
    that yields its KEYS, so the decoder called ``.get()`` on the string "error"
    and the target died of an ``AttributeError`` — after two retries of a
    condition that could not change, and under a message naming nothing an
    operator could act on. A symbol this route does not know is a legitimate
    answer and has to read as one.
    """
    client = make_client(config, json_route({"secdef/search": {"error": "No symbol found"}}))

    assert await client.search_contracts("TOPIX", sec_type="IND") == []
    await client.aclose()


async def test_a_search_row_that_is_not_an_object_is_skipped_not_decoded(config):
    """The same defect one level down: a list is not a list of objects."""
    body = ["nonsense", {"conid": "1", "symbol": "OK"}]
    client = make_client(config, json_route({"secdef/search": body}))

    matches = await client.search_contracts("OK")

    assert [m.conid for m in matches] == [1]
    await client.aclose()


async def test_an_omitted_exchange_is_absent_from_the_wire_and_not_the_string_none(config):
    """What ``history_exchange: null`` actually has to produce.

    Measured [CP 2026-09-10]: SAN (30314144) and TTE (29612193) answer HTTP 500
    ``{"error":"Chart data unavailable"}`` with their listing venue and serve
    daily bars with the exchange omitted — the inversion first seen on TSEJ. The
    declaration is only worth anything if the parameter really does not travel;
    an ``exchange=None`` reaching the query string would be a third, wrong
    request that no response fixture can distinguish from the right one.
    """
    captured = {}

    def handler(request):
        captured.update(dict(request.url.params))
        return httpx.Response(200, json={"symbol": "SAN", "data": []})

    client = make_client(config, handler)
    await client.market_history(30314144, period="1m", bar="1d", exchange=None)
    assert "exchange" not in captured
    assert captured["conid"] == "30314144"
    await client.aclose()


# ── options chains (written new) ──


async def test_option_strikes_are_cast_from_strings(config):
    """Strikes come back as strings; cast before arithmetic, per the playbook."""
    client = make_client(
        config,
        json_route({"/iserver/secdef/strikes": {"call": ["95", "100", "105.5"], "put": ["95"]}}),
    )
    strikes = await client.option_strikes(8314, month="JAN26", exchange="SMART")
    assert strikes["call"] == [95.0, 100.0, 105.5]
    assert all(isinstance(v, float) for v in strikes["call"])
    await client.aclose()


async def test_unparseable_strikes_are_dropped_rather_than_zeroed(config):
    client = make_client(
        config, json_route({"/iserver/secdef/strikes": {"call": ["95", None, "n/a"], "put": []}})
    )
    strikes = await client.option_strikes(8314, month="JAN26", exchange="SMART")
    assert strikes["call"] == [95.0]
    await client.aclose()


async def test_option_contracts_rejects_an_invalid_right(config):
    client = make_client(config, json_route({"/iserver/secdef/info": []}))
    with pytest.raises(ValueError, match="right must be"):
        await client.option_contracts(8314, month="JAN26", exchange="SMART", strike=100, right="X")
    await client.aclose()


# ── /portfolio ──


async def test_portfolio_ledger_and_positions(config):
    client = make_client(
        config,
        json_route(
            {
                "/portfolio/accounts": [{"id": "U1234567"}],
                "/ledger": {
                    "BASE": {"netliquidationvalue": "125000.50", "exchangeRate": 1.0},
                    "USD": {"netliquidationvalue": "40000", "exchangeRate": "0.92"},
                },
            }
        ),
    )
    assert await client.primary_account_id() == "U1234567"
    ledger = await client.ledger("U1234567")
    assert ledger["BASE"]["netliquidationvalue"] == "125000.50"
    await client.aclose()


async def test_no_accounts_is_an_error_not_an_empty_portfolio(config):
    client = make_client(config, json_route({"/portfolio/accounts": []}))
    with pytest.raises(UpstreamError, match="no accounts"):
        await client.primary_account_id()
    await client.aclose()


async def test_all_positions_pages_until_empty(config):
    pages = {0: [{"conid": 1}, {"conid": 2}], 1: [{"conid": 3}], 2: []}

    def handler(request):
        page = int(request.url.path.rsplit("/", 1)[-1])
        return httpx.Response(200, json=pages.get(page, []))

    client = make_client(config, handler)
    rows = await client.all_positions("U1")
    assert [r["conid"] for r in rows] == [1, 2, 3]
    await client.aclose()


# ── error handling ──


async def test_a_401_is_an_auth_error_naming_the_likely_cause(config):
    client = make_client(config, lambda r: httpx.Response(401, json={"error": "not authenticated"}))
    with pytest.raises(AuthError, match="live session token"):
        await client.get("portfolio/accounts")
    await client.aclose()


async def test_a_500_after_retries_raises_rather_than_returning_empty(config):
    client = make_client(config, lambda r: httpx.Response(500, text="boom"))
    with pytest.raises(UpstreamError, match="HTTP 500"):
        await client.get("portfolio/accounts")
    await client.aclose()


# ── HTTP 500 is this route's "no such data", and it costs nine calls ──


def _counting(status: int, payload: dict) -> tuple[list[int], object]:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(status, json=payload)

    return calls, handler


@pytest.mark.parametrize(
    "body",
    ["Chart data unavailable", "Contract details are not available"],
)
async def test_a_500_saying_there_is_no_data_costs_one_call_and_not_three(config, body):
    """Measured 2026-09-10: ``/iserver/marketdata/history`` answers HTTP 500 with
    these bodies for an instrument it holds no bars for. 500 is in
    ``runtime.http.retry_status_codes``, so each of the fifteen permanently absent
    backfill targets spent three attempts here — and two more in the per-target
    loop — being told the same thing nine times."""
    calls, handler = _counting(500, {"error": body})
    client = make_client(config, handler)

    with pytest.raises(PermanentUpstreamError, match="final answer"):
        await client.get("iserver/marketdata/history")

    assert len(calls) == 1
    await client.aclose()


async def test_a_permanent_no_data_error_is_not_retried_per_target_either(config):
    """``with_target_retry`` passes PermanentUpstreamError straight through, so
    the one call is the whole cost rather than a third of it."""
    from obliskiq.collect.retry import TargetRetryPolicy, with_target_retry

    calls, handler = _counting(500, {"error": "Chart data unavailable"})
    client = make_client(config, handler)

    with pytest.raises(PermanentUpstreamError):
        await with_target_retry(
            TargetRetryPolicy.from_config(without_backoff(config)),
            "ASML",
            lambda: client.get("iserver/marketdata/history"),
        )

    assert len(calls) == 1
    await client.aclose()


async def test_an_ordinary_500_is_still_retried_and_is_still_transient(config):
    """The status alone must not decide it. Reading every 500 as permanent turns
    a real outage into a target the collector gives up on after one try."""
    calls, handler = _counting(500, {"error": "boom"})
    client = make_client(config, handler)

    with pytest.raises(UpstreamError) as caught:
        await client.get("portfolio/accounts")

    assert not isinstance(caught.value, PermanentUpstreamError)
    assert len(calls) > 1
    await client.aclose()


async def test_the_accounts_precondition_is_still_repaired_rather_than_refused(config):
    """The two body-matched rules are neighbours and must not collide: one says
    'give up', the other says 'repair the session and try again'."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path.endswith("ssodh/init"):
            return httpx.Response(200, json={"authenticated": True})
        if request.url.path.endswith("accounts"):
            return httpx.Response(200, json={"accounts": ["U1"]})
        # Refused until the brokerage session is re-opened. Re-calling the
        # snapshot does not clear it; ssodh/init does.
        if not any(path.endswith("ssodh/init") for path in seen):
            return httpx.Response(500, json={"error": "Please query /accounts first"})
        return httpx.Response(200, json=[{"conid": 1}])

    client = make_client(config, handler)
    assert await client.get("iserver/marketdata/snapshot") == [{"conid": 1}]
    assert any(path.endswith("ssodh/init") for path in seen)
    await client.aclose()


async def test_a_non_json_body_is_an_error_not_silent_none(config):
    client = make_client(config, lambda r: httpx.Response(200, text="<html>maintenance</html>"))
    with pytest.raises(UpstreamError, match="non-JSON"):
        await client.get("portfolio/accounts")
    await client.aclose()


def _executable_source(module) -> str:
    """Module source with every docstring stripped.

    The docstrings deliberately name what was rejected, so a plain text scan would
    match its own explanation. These tests are about the code."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(module))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                body.pop(0)
    return ast.unparse(tree)


def test_tls_verification_is_never_disabled():
    """IBKR-Portmanteau fell back to verify=False whenever the CA file was absent,
    which is most of the time. The Web API presents a normal public certificate."""
    from obliskiq.ibkr import cp_client

    code = _executable_source(cp_client)
    assert "verify=False" not in code
    assert "ca_bundle if ca_bundle else True" in code


def test_no_tls_bypass_reaches_the_client_constructor(config):
    """A CA bundle may be supplied, but there is no argument that turns verification
    off — passing a path is the only lever."""
    import inspect

    signature = inspect.signature(CpApiClient.__init__)
    assert "ca_bundle" in signature.parameters
    assert not any("verify" in name or "insecure" in name for name in signature.parameters)


def test_the_arbitrary_endpoint_passthrough_was_not_ported():
    """query_ibkr_endpoint guarded writes with a substring blocklist, which fails
    open: /iserver/reply/{id} confirms a live order and contains none of the
    blocked words."""
    from obliskiq.ibkr import cp_client

    code = _executable_source(cp_client)
    assert "query_ibkr_endpoint" not in code
    assert "dangerous_patterns" not in code
    assert not hasattr(CpApiClient, "query_ibkr_endpoint")


def test_no_yahoo_binance_or_coingecko_dependency():
    """Out of scope, and unusable unattended: the chain endpoint is crumb-gated."""
    from obliskiq.ibkr import cp_client

    code = _executable_source(cp_client).lower()
    for host in ("yahoo", "binance", "coingecko", "crumb"):
        assert host not in code


def test_rows_are_tagged_with_their_collection_route():
    assert SOURCE == "cp_api"


# ── brokerage session lifecycle ──
#
# This is the layer that REPLACED IBKR-Portmanteau's gateway auth, and the part
# the unattended collectors depend on. It shipped in the first commit with no
# coverage at all, which is the largest gap the self-review found.


async def test_ssodh_init_opens_the_brokerage_session(config):
    """Required before any /iserver/* call, and the replacement for a human
    having logged into a local gateway.

    It also arms the ``/accounts`` precondition, which the ``/iserver``
    market-data routes refuse to serve without: measured live on 2026-09-10, a
    snapshot after an init that reported authenticated still answered
    ``HTTP 500 {"error":"Please query /accounts first"}``.
    """
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path, dict(request.url.params)))
        if request.url.path.endswith("/iserver/accounts"):
            return httpx.Response(200, json={"accounts": ["U1"], "selectedAccount": "U1"})
        return httpx.Response(200, json={"authenticated": True, "competing": False})

    client = make_client(config, handler)
    assert client.brokerage_session_open is False
    assert await client.init_brokerage_session() is True
    assert client.brokerage_session_open is True

    method, path, params = seen[0]
    assert path.endswith("/iserver/auth/ssodh/init")
    assert method == "POST"
    assert params == {"publish": "true", "compete": "true"}
    # The arming call follows the init, in that order: it is an /iserver call
    # itself and the brokerage session has to be open before it is made.
    assert seen[1][0] == "GET"
    assert seen[1][1].endswith("/iserver/accounts")
    await client.aclose()


async def test_an_unarmed_account_is_rearmed_and_the_call_repeated(config):
    """The ``/accounts`` precondition is lost mid-session, not just missing at the
    start, and ordinary retrying does not clear it — only calling /accounts again
    does. Measured live 2026-09-10: every chain target failed this way after an
    init that had already armed successfully."""
    seen = []

    def handler(request):
        path = request.url.path
        seen.append(path)
        if path.endswith("/iserver/marketdata/snapshot"):
            # Refuse until the accounts endpoint has been called again.
            if not any(p.endswith("/iserver/accounts") for p in seen):
                return httpx.Response(500, json={"error": "Please query /accounts first"})
            return httpx.Response(200, json=[{"conid": 1, "31": "10.0"}])
        return httpx.Response(200, json={"accounts": ["U1"]})

    client = make_client(config, handler)
    body = await client.request("GET", "iserver/marketdata/snapshot", params={"conids": "1"})
    assert body == [{"conid": 1, "31": "10.0"}]
    # The arming call lands between the refused snapshots and the one that
    # served: ordinary retries alone never clear the precondition.
    armed_at = next(i for i, p in enumerate(seen) if p.endswith("/iserver/accounts"))
    last_snapshot = max(i for i, p in enumerate(seen) if p.endswith("/iserver/marketdata/snapshot"))
    assert armed_at < last_snapshot
    await client.aclose()


async def test_a_bare_401_rearms_the_session_and_repeats_the_call(config):
    """The same loss wearing a different status code.

    Measured live 2026-09-10: one chain target answered ``HTTP 401`` on
    /iserver/marketdata/snapshot 31 minutes into a run, with roughly 22 hours left
    on the live session token and with every target after it collecting normally.
    A token that had genuinely expired would have taken the rest of the run with
    it; a dropped ``/iserver`` session is what recovers on its own. Before this,
    the 401 raised AuthError immediately and the target was simply lost, while the
    repair — ssodh/init — sat one branch away.
    """
    seen = []

    def handler(request):
        path = request.url.path
        seen.append(path)
        if path.endswith("/iserver/marketdata/snapshot"):
            if not any(p.endswith("/ssodh/init") for p in seen):
                return httpx.Response(401, json={"error": "not authenticated"})
            return httpx.Response(200, json=[{"conid": 1, "31": "10.0"}])
        return httpx.Response(200, json={"authenticated": True})

    client = make_client(config, handler)
    body = await client.request("GET", "iserver/marketdata/snapshot", params={"conids": "1"})

    assert body == [{"conid": 1, "31": "10.0"}]
    assert any(p.endswith("/ssodh/init") for p in seen)
    await client.aclose()


async def test_a_401_that_survives_the_rearm_still_raises(config):
    """One re-arm, not a loop. A credential that is actually wrong must fail, and
    fail saying so, rather than re-initialising the session forever."""
    client = make_client(config, json_route({"": (401, {"error": "no"})}, default_status=401))

    with pytest.raises(AuthError, match="rejected with HTTP 401"):
        await client.request("GET", "iserver/marketdata/snapshot", params={"conids": "1"})
    await client.aclose()


async def test_a_403_is_not_rearmed_because_it_is_not_a_dropped_session(config):
    """403 is an entitlement answer, and re-arming cannot change an entitlement."""
    seen = []

    def handler(request):
        seen.append(request.url.path)
        return httpx.Response(403, json={"error": "forbidden"})

    client = make_client(config, handler)
    with pytest.raises(AuthError, match="rejected with HTTP 403"):
        await client.request("GET", "iserver/marketdata/snapshot", params={"conids": "1"})
    assert not any(p.endswith("/ssodh/init") for p in seen)
    await client.aclose()


async def test_an_unauthenticated_init_does_not_claim_a_session(config):
    client = make_client(config, json_route({"/ssodh/init": {"authenticated": False}}))
    assert await client.init_brokerage_session() is False
    assert client.brokerage_session_open is False
    await client.aclose()


async def test_an_init_response_without_the_field_is_treated_as_closed(config):
    """Absent is not authenticated. Assuming otherwise would have every later
    call fail with an opaque 401."""
    client = make_client(config, json_route({"/ssodh/init": {}}))
    assert await client.init_brokerage_session() is False
    await client.aclose()


async def test_tickle_keeps_the_session_alive(config):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        assert request.method == "POST"
        return httpx.Response(200, json={"session": "abc", "iserver": {"authStatus": {}}})

    client = make_client(config, handler)
    assert await client.tickle() == {"session": "abc", "iserver": {"authStatus": {}}}
    assert calls["n"] == 1
    await client.aclose()


async def test_keepalive_ticks_on_the_configured_interval(config, monkeypatch):
    """The loop is driven by ibkr.cp_api.session.tickle_interval_seconds. Time is
    replaced rather than waited on, so the test asserts behaviour, not duration."""
    import asyncio
    import copy

    from obliskiq.config import Config

    data = copy.deepcopy(config.as_dict())
    data["ibkr"]["cp_api"]["session"]["tickle_interval_seconds"] = 0.001
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json={})

    client = make_client(Config(data), handler)
    await client.start_keepalive()
    for _ in range(200):
        if calls["n"] >= 3:
            break
        await asyncio.sleep(0.001)
    await client.stop_keepalive()
    assert calls["n"] >= 3
    await client.aclose()


async def test_starting_keepalive_twice_does_not_start_two_loops(config):
    import copy

    from obliskiq.config import Config

    data = copy.deepcopy(config.as_dict())
    data["ibkr"]["cp_api"]["session"]["tickle_interval_seconds"] = 60.0

    client = make_client(Config(data), json_route({"/tickle": {}}))
    await client.start_keepalive()
    first = client._tickle_task
    await client.start_keepalive()
    assert client._tickle_task is first
    await client.stop_keepalive()
    await client.aclose()


async def test_stopping_keepalive_that_never_started_is_a_no_op(config):
    client = make_client(config, json_route({"/tickle": {}}))
    await client.stop_keepalive()
    await client.aclose()


async def test_a_failing_tickle_marks_the_session_closed_and_re_inits(config):
    """The original merely flipped a `healthy` flag every caller had to remember
    to consult. A run of failures must actually rebuild the session."""
    import asyncio
    import copy

    from obliskiq.config import Config

    data = copy.deepcopy(config.as_dict())
    data["ibkr"]["cp_api"]["session"]["tickle_interval_seconds"] = 0.001
    data["ibkr"]["cp_api"]["session"]["tickle_failure_grace"] = 2

    state = {"tickles": 0, "inits": 0}

    def handler(request):
        if request.url.path.endswith("/tickle"):
            state["tickles"] += 1
            return httpx.Response(503, text="gone")
        if "ssodh/init" in request.url.path:
            state["inits"] += 1
            return httpx.Response(200, json={"authenticated": True})
        return httpx.Response(404, json={})

    client = make_client(Config(data), handler)
    client._brokerage_session_open = True
    await client.start_keepalive()
    for _ in range(400):
        if state["inits"] >= 1:
            break
        await asyncio.sleep(0.001)
    await client.stop_keepalive()

    assert state["inits"] >= 1, "a run of tickle failures must re-init the session"
    assert state["tickles"] >= 2, "re-init must not fire before the configured grace"
    await client.aclose()


async def test_aclose_stops_the_keepalive_loop(config):
    import copy

    from obliskiq.config import Config

    data = copy.deepcopy(config.as_dict())
    data["ibkr"]["cp_api"]["session"]["tickle_interval_seconds"] = 60.0

    client = make_client(Config(data), json_route({"/tickle": {}}))
    await client.start_keepalive()
    await client.aclose()
    assert client._tickle_task is None


async def test_the_client_works_as_an_async_context_manager(config):
    async with make_client(config, json_route({"/portfolio/accounts": [{"id": "U1"}]})) as client:
        assert await client.primary_account_id() == "U1"


async def test_auth_status_is_posted_not_got(config):
    """CP's /iserver/auth/status is a POST, unlike most read endpoints."""
    seen = {}

    def handler(request):
        seen["method"] = request.method
        return httpx.Response(200, json={"authenticated": True, "connected": True})

    client = make_client(config, handler)
    assert (await client.auth_status())["authenticated"] is True
    assert seen["method"] == "POST"
    await client.aclose()


async def test_an_empty_body_decodes_to_none_rather_than_raising(config):
    client = make_client(config, lambda r: httpx.Response(200, content=b""))
    assert await client.get("tickle") is None
    await client.aclose()
