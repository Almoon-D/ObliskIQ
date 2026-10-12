"""Contract identity: one listing pinned by conid, venue and currency; corrections supersede.

Ported in intent from ObliskIQ-lite-MCP's ``tests/test_collect_conid_supersede.py`` (the universe
sync itself arrives in Session 8): a corrected conid retires the row it replaces and deletes
nothing; a run that changes no conid retires nothing; a symbol that failed to resolve retires
nothing.
"""

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from obliskiq.ibkr.client import Secdef
from obliskiq.ibkr.errors import AmbiguousContractError
from obliskiq.ibkr.identity import reconcile_identity, resolve_identity
from obliskiq.ibkr.models import ContractIdentity
from obliskiq.store import repos

BASE = "https://gw.test/v1/api"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "ibkr"
OLD_CONID = 117902840  # the NASDAQ NY-registered ASML line
NEW_CONID = 117589399  # the AEB listing
AT = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)


def _fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def secdef(make_session, shipped_settings):
    return Secdef(make_session(), shipped_settings.ibkr)


@respx.mock
async def test_the_declared_venue_resolves_the_european_listing(secdef):
    respx.post(f"{BASE}/iserver/secdef/search").mock(
        return_value=httpx.Response(200, json=_fixture("search_asml.json"))
    )
    respx.get(f"{BASE}/iserver/contract/{NEW_CONID}/info").mock(
        return_value=httpx.Response(200, json=_fixture("contract_info_asml.json"))
    )
    identity = await resolve_identity(secdef, "ASML", listing_exchange="AEB", currency="EUR")
    assert identity == ContractIdentity(
        conid=NEW_CONID, symbol="ASML", listing_exchange="AEB", currency="EUR"
    )


@respx.mock
async def test_several_listings_without_a_matching_venue_are_refused(secdef):
    respx.post(f"{BASE}/iserver/secdef/search").mock(
        return_value=httpx.Response(200, json=_fixture("search_asml.json"))
    )
    with pytest.raises(AmbiguousContractError, match="resolved to 3 contracts"):
        await resolve_identity(secdef, "ASML", listing_exchange="SBF", currency="EUR")


@respx.mock
async def test_a_symbol_that_matches_nothing_is_refused(secdef):
    respx.post(f"{BASE}/iserver/secdef/search").mock(
        return_value=httpx.Response(200, json=_fixture("search_miss.json"))
    )
    with pytest.raises(AmbiguousContractError, match="resolved to 0 contracts"):
        await resolve_identity(secdef, "TOPIX", listing_exchange="TSEJ", currency="JPY")


@respx.mock
async def test_a_currency_that_differs_from_the_declaration_is_refused(secdef):
    respx.post(f"{BASE}/iserver/secdef/search").mock(
        return_value=httpx.Response(200, json=_fixture("search_asml.json"))
    )
    respx.get(f"{BASE}/iserver/contract/{NEW_CONID}/info").mock(
        return_value=httpx.Response(200, json=_fixture("contract_info_asml.json"))
    )
    with pytest.raises(AmbiguousContractError, match="not the declared USD"):
        await resolve_identity(secdef, "ASML", listing_exchange="AEB", currency="USD")


@respx.mock
async def test_a_pinned_conid_is_confirmed_without_a_search(secdef):
    search = respx.post(f"{BASE}/iserver/secdef/search")
    respx.get(f"{BASE}/iserver/contract/{NEW_CONID}/info").mock(
        return_value=httpx.Response(200, json=_fixture("contract_info_asml.json"))
    )
    identity = await resolve_identity(
        secdef, "ASML", listing_exchange="AEB", currency="EUR", pinned_conid=NEW_CONID
    )
    assert identity.conid == NEW_CONID
    assert not search.called


@respx.mock
async def test_a_pinned_conid_on_another_venue_is_refused(secdef):
    info = _fixture("contract_info_asml.json")
    info["valid_exchanges"] = "SMART,NASDAQ"
    respx.get(f"{BASE}/iserver/contract/{OLD_CONID}/info").mock(
        return_value=httpx.Response(200, json=info)
    )
    with pytest.raises(AmbiguousContractError, match="not listed on the declared AEB"):
        await resolve_identity(
            secdef, "ASML", listing_exchange="AEB", currency="EUR", pinned_conid=OLD_CONID
        )


@respx.mock
async def test_a_declared_listing_exchange_field_wins_over_the_valid_list(secdef):
    info = _fixture("contract_info_asml.json")
    info["listingExchange"] = "NASDAQ"
    respx.get(f"{BASE}/iserver/contract/{NEW_CONID}/info").mock(
        return_value=httpx.Response(200, json=info)
    )
    with pytest.raises(AmbiguousContractError):
        await resolve_identity(
            secdef, "ASML", listing_exchange="AEB", currency="EUR", pinned_conid=NEW_CONID
        )


# ── supersession ──


@pytest.fixture
def seeded(store, make_instrument):
    old = make_instrument(conid=OLD_CONID, symbol="ASML", listing_exchange="NASDAQ")
    new = make_instrument(conid=NEW_CONID, symbol="ASML", listing_exchange="AEB", currency="EUR")
    repos.upsert_instrument(store, old)
    repos.upsert_instrument(store, new)
    return store


def _identity(conid: int) -> ContractIdentity:
    return ContractIdentity(conid=conid, symbol="ASML", listing_exchange="AEB", currency="EUR")


def test_a_corrected_conid_supersedes_the_row_it_replaces(seeded):
    assert reconcile_identity(seeded, held_conid=OLD_CONID, resolved=_identity(NEW_CONID), at=AT)
    old = repos.get_instrument(seeded, OLD_CONID, as_of=date(2026, 10, 13))
    assert old is not None, "the superseded row is not deleted"
    assert old.superseded_by == NEW_CONID


def test_history_collected_against_the_old_conid_survives(seeded, make_bar):
    repos.upsert_bars(seeded, [make_bar(conid=OLD_CONID)])
    reconcile_identity(seeded, held_conid=OLD_CONID, resolved=_identity(NEW_CONID), at=AT)
    assert repos.bar_count(seeded, OLD_CONID, as_of=date(2026, 10, 13)) == 1


def test_a_run_that_changes_no_conid_supersedes_nothing(seeded):
    assert not reconcile_identity(
        seeded, held_conid=NEW_CONID, resolved=_identity(NEW_CONID), at=AT
    )
    row = repos.get_instrument(seeded, NEW_CONID, as_of=date(2026, 10, 13))
    assert row is not None
    assert row.superseded_by is None


def test_a_symbol_that_failed_to_resolve_supersedes_nothing(seeded):
    assert not reconcile_identity(seeded, held_conid=OLD_CONID, resolved=None, at=AT)
    row = repos.get_instrument(seeded, OLD_CONID, as_of=date(2026, 10, 13))
    assert row is not None
    assert row.superseded_by is None


def test_a_replacement_not_in_the_store_is_refused(store, make_instrument):
    repos.upsert_instrument(store, make_instrument(conid=OLD_CONID, symbol="ASML"))
    with pytest.raises(AmbiguousContractError, match="not in the store"):
        reconcile_identity(store, held_conid=OLD_CONID, resolved=_identity(NEW_CONID), at=AT)
