"""
Tests for the Polymarket client (Asymmetry Hunter component g).
===============================================================

Recorded/mock Gamma API payloads — no live network calls. Covers parsing of the
JSON-encoded outcome/price lists, snapshot persistence, and velocity computation
against a fake warehouse.
"""

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from modules.market.polymarket_client import PolymarketClient

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Recorded Gamma payloads + fakes
# ---------------------------------------------------------------------------
def _gamma_market(yes_price="0.82"):
    """A single Gamma /markets?slug= response element (returned as a list)."""
    return [
        {
            "slug": "us-recession-in-2026",
            "question": "US recession in 2026?",
            "outcomes": '["Yes", "No"]',
            "outcomePrices": f'["{yes_price}", "{round(1 - float(yes_price), 2)}"]',
        }
    ]


class FakeSession:
    """Returns queued payloads in order; records the URLs/params requested."""

    def __init__(self, payloads):
        self._payloads = list(payloads)
        self.calls = []

    def request(self, method, url, params=None, headers=None, json=None, timeout=None):
        self.calls.append((method, url, params))
        payload = self._payloads.pop(0) if self._payloads else None
        return _FakeResponse(payload)


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.status = 200 if payload is not None else 404
        self.headers = {}
        self.request_info = None
        self.history = ()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def text(self):
        return "" if self._payload is None else "ok"

    async def json(self):
        return self._payload


class FakeWarehouse:
    def __init__(self, prior=None):
        self._prior = prior or {}
        self.stored = []

    def get_polymarket_velocity(self, market_id, current_prob=None):
        prior = self._prior.get(market_id)
        if prior is None or current_prob is None:
            return None
        return current_prob - prior

    def store_polymarket_snapshot(self, rows):
        self.stored.extend(rows)
        return len(rows)


def _client(tmp_config):
    return PolymarketClient(config_path=tmp_config, warehouse=FakeWarehouse())


@pytest.fixture
def curated_config(tmp_path):
    cfg = tmp_path / "polymarket_markets.json"
    cfg.write_text(
        '{"markets": [{"id": "us-recession-2026", '
        '"slug": "us-recession-in-2026", "question": "US recession in 2026?", '
        '"yes_means": "recession", "data_stance": "recession_regime"}]}'
    )
    return cfg


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_parse_yes_probability_from_encoded_lists():
    prob = PolymarketClient._parse_yes_probability(_gamma_market("0.82")[0])
    assert prob == pytest.approx(0.82)


@pytest.mark.asyncio
async def test_fetch_snapshot_parses_curated_markets(curated_config):
    client = _client(curated_config)
    session = FakeSession([_gamma_market("0.75")])
    markets = await client.fetch_snapshot(session)
    assert len(markets) == 1
    m = markets[0]
    assert m["id"] == "us-recession-2026"
    assert m["prob_yes"] == pytest.approx(0.75)
    assert m["data_stance"] == "recession_regime"
    # One HTTP call, keyed by slug.
    assert session.calls[0][2] == {"slug": "us-recession-in-2026"}


@pytest.mark.asyncio
async def test_unreachable_market_is_skipped(curated_config):
    client = _client(curated_config)
    session = FakeSession([None])  # 404 / no payload
    markets = await client.fetch_snapshot(session)
    assert markets == []


def test_persist_computes_velocity_against_prior():
    wh = FakeWarehouse(prior={"us-recession-2026": 0.60})
    client = PolymarketClient(warehouse=wh)
    markets = [
        {"id": "us-recession-2026", "question": "US recession in 2026?", "prob_yes": 0.75}
    ]
    client.persist(markets, snapshot_date=date(2026, 6, 16))
    assert markets[0]["velocity"] == pytest.approx(0.15)
    assert wh.stored and wh.stored[0]["market_id"] == "us-recession-2026"
    assert wh.stored[0]["velocity"] == pytest.approx(0.15)


def test_persist_no_prior_yields_none_velocity():
    wh = FakeWarehouse(prior={})
    client = PolymarketClient(warehouse=wh)
    markets = [{"id": "x", "question": "q", "prob_yes": 0.5}]
    client.persist(markets, snapshot_date=date(2026, 6, 16))
    assert markets[0]["velocity"] is None


def test_missing_config_degrades_to_empty(tmp_path):
    client = PolymarketClient(config_path=tmp_path / "does_not_exist.json")
    assert client.markets == []
