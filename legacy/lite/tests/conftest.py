"""Shared fixtures.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from obliskiq.config import load_config  # noqa: E402
from obliskiq.store import open_store  # noqa: E402


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return ROOT


@pytest.fixture(scope="session")
def config():
    """The real config.yaml. Tests read the shipped file, not a stub, so a config
    change that breaks a loader is caught here rather than in production."""
    return load_config(ROOT / "config.yaml")


def without_backoff(config):
    """The same config with HTTP backoff removed.

    Retry behaviour is asserted by call count, not by wall-clock time, so tests
    must not spend seconds sleeping through real exponential backoff.
    """
    import copy

    from obliskiq.config import Config

    data = copy.deepcopy(config.as_dict())
    data["http"].update(
        {"base_delay_seconds": 0.0, "max_delay_seconds": 0.0, "jitter_seconds": 0.0}
    )
    return Config(data)


@pytest.fixture()
def fast_config(config):
    return without_backoff(config)


@pytest.fixture()
def store(tmp_path, config):
    with open_store(tmp_path / "test.db", config) as opened:
        yield opened


@pytest.fixture()
def seeded_store(store):
    """A store with one universe row, so foreign keys resolve."""
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO universe (contract_id, symbol, currency, asset_class, market, sector) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (1, "TEST", "EUR", "STK", "europe", "industrials"),
        )
    return store


@pytest.fixture()
def server_settings(tmp_path):
    """Environment for the MCP server, pointed at the same store as ``store``.

    Built by hand rather than through ``load_settings`` so a test never depends on
    the developer's own environment — and so the auth rules are exercised by the
    tests that mean to, not accidentally by every other one.
    """
    from obliskiq.env import Settings

    return Settings(
        db_path=tmp_path / "test.db",
        config_path=ROOT / "config.yaml",
        log_level="INFO",
        log_format="json",
        timezone="UTC",
        mcp_host="127.0.0.1",
        mcp_port=8000,
        mcp_auth_token=None,
        fred_api_key=None,
        eia_api_key=None,
        marketdata_api_token=None,
        ibkr_cp_base_url=None,
        ibkr_connector_url=None,
        ibkr_consumer_key=None,
        ibkr_access_token=None,
        ibkr_access_token_secret=None,
        ibkr_signature_key_path=None,
        ibkr_encryption_key_path=None,
        ibkr_dh_param_path=None,
        ibkr_ca_bundle=None,
    )


@pytest.fixture()
def server_context(store, config, server_settings):
    """A ServerContext over the ``store`` fixture's database.

    ``store`` stays open for the test while the context opens its own short-lived
    connection per call — which is the arrangement the server actually runs in,
    so a test exercises the real concurrency shape rather than a simplified one.
    """
    from obliskiq.server.context import ServerContext

    return ServerContext(settings=server_settings, config=config)


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    """Recorded IBKR responses. Regenerate with tests/fixtures/build_fixtures.py."""
    return ROOT / "tests" / "fixtures" / "ibkr"


@pytest.fixture()
def market_data(fixtures_dir):
    """A MarketData over the recorded fixtures.

    A missing fixture raises rather than returning empty: a test that passes
    because its fixture was never found is the same bug the collectors exist to
    prevent, wearing a lab coat.
    """
    from obliskiq.collect.source import FixtureMarketData

    return FixtureMarketData(fixtures_dir)


@pytest.fixture()
def fast_collect_config(config):
    """Config with every sleep removed.

    Retry and pacing behaviour is asserted by call count, not by wall-clock time,
    so the suite must not spend real seconds backing off.
    """
    import copy

    from obliskiq.config import Config

    data = copy.deepcopy(config.as_dict())
    data["http"].update(
        {"base_delay_seconds": 0.0, "max_delay_seconds": 0.0, "jitter_seconds": 0.0}
    )
    data["collectors"].update(
        {
            "target_backoff_seconds": 0.0,
            "target_backoff_max_seconds": 0.0,
            "request_spacing_seconds": 0.0,
        }
    )
    return Config(data)


#: The session every collector test runs "on". Fixed so the tenor window, the
#: expiry filtering and the bar history all line up deterministically.
FIXTURE_SESSION = date(2026, 1, 15)


@pytest.fixture()
def session_date() -> date:
    return FIXTURE_SESSION


@pytest.fixture()
def seeded_universe(store, fast_collect_config, market_data):
    """A store with universe.yaml resolved into it, ready for a collector."""
    import asyncio

    from obliskiq.collect.runlog import CollectorRun
    from obliskiq.collect.universe import UniverseSync

    async def seed():
        with CollectorRun(store, "universe") as run:
            await UniverseSync(store, fast_collect_config, market_data).run(run=run)
        return run

    asyncio.run(seed())
    return store
