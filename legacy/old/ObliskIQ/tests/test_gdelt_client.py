"""
Tests for the GDELT client (Asymmetry Hunter component h).
==========================================================

Recorded/mock GDELT 2.0 DOC API timeline payloads — no live network. Covers
timeline parsing, the Welford incremental z-score, anomaly flagging (volume
spike + negative tone), and warm-up degradation.
"""

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from modules.market.gdelt_client import GIVE_UP_AFTER_DARK_REGIONS, GDELTClient

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Recorded GDELT timeline payloads + fakes
# ---------------------------------------------------------------------------
def _timeline(value):
    return {"timeline": [{"series": "s", "data": [
        {"date": "20260614T000000Z", "value": value * 0.9},
        {"date": "20260615T000000Z", "value": value},
    ]}]}


class FakeSession:
    """Returns queued timeline payloads in order (volume, tone, volume, tone...)."""

    def __init__(self, payloads):
        self._payloads = list(payloads)

    def request(self, method, url, params=None, headers=None, json=None, timeout=None):
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
    """In-memory Welford store + captured pulse rows."""

    def __init__(self, stats=None):
        self._stats = stats or {}
        self.pulse_rows = []

    def get_gdelt_running_stats(self, region, metric):
        return self._stats.get((region, metric))

    def upsert_gdelt_running_stats(self, region, metric, count, mean, m2):
        self._stats[(region, metric)] = {"count": count, "mean": mean, "m2": m2}

    def store_gdelt_pulse(self, rows):
        self.pulse_rows.extend(rows)
        return len(rows)


@pytest.fixture
def one_region_config(tmp_path):
    cfg = tmp_path / "gdelt_regions.json"
    cfg.write_text(
        '{"regions": {"MIDDLE_EAST": {"query": "(Iran OR Israel)", "theme": "conflict"}},'
        ' "asset_map": {"MIDDLE_EAST": {"USO": "long"}}}'
    )
    return cfg


def _mature_volume_stats():
    """count=20, mean=100, std=10 -> today's 150 reads as +5σ."""
    # variance = m2 / (count-1); want std=10 -> variance=100 -> m2 = 100 * 19.
    return {"count": 20.0, "mean": 100.0, "m2": 100.0 * 19.0}


# ---------------------------------------------------------------------------
# Welford unit
# ---------------------------------------------------------------------------
def test_welford_update_z_score():
    stats = _mature_volume_stats()
    new_stats, z = GDELTClient._welford_update(stats, 150.0)
    assert z == pytest.approx(5.0, abs=0.01)
    assert new_stats["count"] == 21.0


def test_welford_no_z_until_two_observations():
    new_stats, z = GDELTClient._welford_update(None, 100.0)
    assert z is None
    assert new_stats["count"] == 1.0


def test_latest_timeline_value_takes_last_point():
    assert GDELTClient._latest_timeline_value(_timeline(123.0)) == pytest.approx(123.0)
    assert GDELTClient._latest_timeline_value({"timeline": []}) is None
    assert GDELTClient._latest_timeline_value(None) is None


# ---------------------------------------------------------------------------
# fetch_pulse integration (mocked HTTP + warehouse)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_anomaly_detected_on_volume_spike_negative_tone(one_region_config):
    wh = FakeWarehouse(stats={
        ("MIDDLE_EAST", "volume"): _mature_volume_stats(),
        ("MIDDLE_EAST", "tone"): {"count": 20.0, "mean": 1.0, "m2": 19.0},
    })
    client = GDELTClient(config_path=one_region_config, warehouse=wh, min_history=20)
    # volume=150 (spike, +5σ), tone=-4 (negative stress).
    session = FakeSession([_timeline(150.0), _timeline(-4.0)])
    pulse = await client.fetch_pulse(session, snapshot_date=date(2026, 6, 16))
    assert pulse["status"] == "success"
    region = pulse["regions"]["MIDDLE_EAST"]
    assert region["anomalous"] is True
    assert region["volume_z"] == pytest.approx(5.0, abs=0.1)
    assert pulse["worst_region"] == "MIDDLE_EAST"
    assert wh.pulse_rows  # snapshot persisted


@pytest.mark.asyncio
async def test_warming_up_when_history_immature(one_region_config):
    wh = FakeWarehouse(stats={
        ("MIDDLE_EAST", "volume"): {"count": 3.0, "mean": 100.0, "m2": 200.0},
        ("MIDDLE_EAST", "tone"): {"count": 3.0, "mean": 0.0, "m2": 5.0},
    })
    client = GDELTClient(config_path=one_region_config, warehouse=wh, min_history=20)
    session = FakeSession([_timeline(150.0), _timeline(-4.0)])
    pulse = await client.fetch_pulse(session, snapshot_date=date(2026, 6, 16))
    region = pulse["regions"]["MIDDLE_EAST"]
    assert region["status"] == "warming_up"
    assert region["anomalous"] is False
    assert pulse["status"] == "warming_up"


@pytest.mark.asyncio
async def test_positive_tone_is_not_anomalous(one_region_config):
    wh = FakeWarehouse(stats={
        ("MIDDLE_EAST", "volume"): _mature_volume_stats(),
        ("MIDDLE_EAST", "tone"): {"count": 20.0, "mean": 1.0, "m2": 19.0},
    })
    client = GDELTClient(config_path=one_region_config, warehouse=wh, min_history=20)
    # Volume spikes but tone is POSITIVE -> not geopolitical stress.
    session = FakeSession([_timeline(150.0), _timeline(3.0)])
    pulse = await client.fetch_pulse(session, snapshot_date=date(2026, 6, 16))
    assert pulse["regions"]["MIDDLE_EAST"]["anomalous"] is False
    assert pulse["worst_region"] is None


@pytest.mark.asyncio
async def test_unreachable_region_marked_unavailable(one_region_config):
    wh = FakeWarehouse()
    client = GDELTClient(config_path=one_region_config, warehouse=wh)
    session = FakeSession([None, None])
    pulse = await client.fetch_pulse(session, snapshot_date=date(2026, 6, 16))
    assert pulse["regions"]["MIDDLE_EAST"] == {"status": "unavailable"}


class CountingSession(FakeSession):
    def __init__(self, payloads):
        super().__init__(payloads)
        self.requests = 0

    def request(self, *args, **kwargs):
        self.requests += 1
        return super().request(*args, **kwargs)


@pytest.fixture
def five_region_config(tmp_path):
    import json

    cfg = tmp_path / "gdelt_regions.json"
    regions = {name: {"query": f"({name})", "theme": "conflict"} for name in ("A", "B", "C", "D", "E")}
    cfg.write_text(json.dumps({"regions": regions, "asset_map": {}}))
    return cfg


@pytest.mark.asyncio
async def test_a_throttled_feed_is_not_hammered_for_every_region(five_region_config):
    """Run #184: GDELT answered 429 to all 5 regions x 2 queries and each spent ~70 s in retry backoff (~7 min)."""
    client = GDELTClient(config_path=five_region_config, warehouse=FakeWarehouse())
    session = CountingSession([])  # every request fails

    pulse = await client.fetch_pulse(session, snapshot_date=date(2026, 6, 16))

    assert session.requests == 2 * GIVE_UP_AFTER_DARK_REGIONS  # asked for two regions, then stopped
    assert all(r == {"status": "unavailable"} for r in pulse["regions"].values())
    assert set(pulse["regions"]) == {"A", "B", "C", "D", "E"}  # the skipped ones are reported, not dropped
    assert pulse["status"] == "warming_up" and pulse["worst_region"] is None


@pytest.mark.asyncio
async def test_one_good_region_resets_the_give_up_counter(five_region_config):
    client = GDELTClient(config_path=five_region_config, warehouse=FakeWarehouse())
    # A dark (2 requests), B live (2), C dark (2), D live (2), E dark (2): never two dark regions in a row.
    session = CountingSession([None, None, _timeline(10.0), _timeline(1.0), None, None, _timeline(10.0), _timeline(1.0)])

    pulse = await client.fetch_pulse(session, snapshot_date=date(2026, 6, 16))

    assert session.requests == 10
    assert pulse["regions"]["A"] == {"status": "unavailable"}
    assert pulse["regions"]["B"]["status"] in ("warming_up", "success")
    assert pulse["regions"]["D"]["status"] in ("warming_up", "success")
