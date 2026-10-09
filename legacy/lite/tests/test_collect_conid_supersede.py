"""Correcting a conid retires the row it replaces, and deletes nothing.

`ASML` resolved to the NASDAQ NY-registered line under a universe row declaring
Amsterdam and EUR, and collected that listing's chain for a whole session.
Pinning the right conid fixes the declaration — and, on its own, would leave the
old row in the store with its collection flags still set, so both listings would
be walked under one symbol forever.

Deleting it is not the alternative. ``bars``, ``chain_snapshots`` and
``curve_snapshots`` all cascade off ``universe``, so a delete would take the
irreplaceable history collected against that conid with it. The flags come off;
the record stays.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import copy

import pytest

from obliskiq.collect.runlog import CollectorRun
from obliskiq.collect.source import FixtureMarketData
from obliskiq.collect.universe import ORIGIN_DECLARED, ORIGIN_PORTFOLIO, UniverseSync
from obliskiq.config import Config

OLD_CONID = 117902840  # the NASDAQ NY-registered ASML line
NEW_CONID = 117589399  # the AEB listing universe.yaml declares


def fast(config) -> Config:
    data = copy.deepcopy(config.as_dict())
    data["collectors"].update(
        {
            "target_backoff_seconds": 0.0,
            "target_backoff_max_seconds": 0.0,
            "request_spacing_seconds": 0.0,
        }
    )
    return Config(data)


def insert_row(store, conid: int, symbol: str, *, origin: str = ORIGIN_DECLARED) -> None:
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO universe (contract_id, symbol, currency, asset_class, market, "
            "sector, origin, collect_chain, collect_bars) "
            "VALUES (?, ?, 'EUR', 'STK', 'eurozone', 'technology', ?, 1, 1)",
            (conid, symbol, origin),
        )


def flags(store, conid: int) -> tuple[int, int]:
    row = store.execute(
        "SELECT collect_chain, collect_bars FROM universe WHERE contract_id = ?", (conid,)
    ).fetchone()
    return (row[0], row[1])


async def sync(store, config, market_data) -> CollectorRun:
    with CollectorRun(store, "universe") as run:
        await UniverseSync(store, config, market_data).run(run=run)
    return run


@pytest.fixture()
def superseded(store, config, market_data):
    """A store already holding ASML under the conid the resolver used to pick."""
    insert_row(store, OLD_CONID, "ASML")
    return store


async def test_the_superseded_row_stops_being_collected(superseded, config, market_data):
    run = await sync(superseded, fast(config), market_data)

    assert flags(superseded, OLD_CONID) == (0, 0)
    assert run.notes["superseded_rows_retired"] == 1


async def test_the_row_that_replaces_it_collects(superseded, config, market_data):
    await sync(superseded, fast(config), market_data)

    assert flags(superseded, NEW_CONID) == (1, 1)


async def test_the_superseded_row_is_not_deleted(superseded, config, market_data):
    """History cascades off this table. A tidy-up delete is a data loss."""
    await sync(superseded, fast(config), market_data)

    row = superseded.execute(
        "SELECT symbol FROM universe WHERE contract_id = ?", (OLD_CONID,)
    ).fetchone()
    assert row is not None and row[0] == "ASML"


async def test_history_collected_against_the_old_conid_survives(superseded, config, market_data):
    with superseded.transaction() as connection:
        connection.execute(
            "INSERT INTO chain_snapshots (snapshot_date, underlying_id, expiry, strike, "
            "right, source) VALUES ('2026-09-10', ?, '2026-12-18', 900.0, 'C', 'cp_api')",
            (OLD_CONID,),
        )

    await sync(superseded, fast(config), market_data)

    kept = superseded.execute(
        "SELECT COUNT(*) FROM chain_snapshots WHERE underlying_id = ?", (OLD_CONID,)
    ).fetchone()[0]
    assert kept == 1


async def test_a_run_that_changes_no_conid_retires_nothing(store, config, market_data):
    run = await sync(store, fast(config), market_data)

    assert run.notes["superseded_rows_retired"] == 0


async def test_a_symbol_that_failed_to_resolve_retires_nothing(store, config, fixtures_dir):
    """The narrow part of the rule. A row is retired only when its symbol
    RESOLVED, elsewhere — never because an upstream outage left it unresolved,
    which would turn a bad afternoon into a universe that stops collecting.

    ``SPY`` is declared without a pinned conid, so it goes through the resolver
    and this makes the resolver fail.
    """
    stale = 999_000_002
    insert_row(store, stale, "SPY")

    class NoResolution(FixtureMarketData):
        async def resolve_conid(self, symbol, *, sec_type="STK"):
            if symbol.upper() == "SPY":
                raise RuntimeError("upstream down")
            return await super().resolve_conid(symbol, sec_type=sec_type)

    run = await sync(store, fast(config), NoResolution(fixtures_dir))

    assert flags(store, stale) == (1, 1)
    assert run.notes["superseded_rows_retired"] == 0


async def test_a_portfolio_row_is_left_to_its_own_reconciliation(store, config, market_data):
    """Two rules, two origins. A portfolio row retires on ``last_seen_at``; this
    one fires on a conid correction, and must not reach across."""
    insert_row(store, 999_000_001, "SPY", origin=ORIGIN_PORTFOLIO)

    run = await sync(store, fast(config), market_data)

    assert run.notes["superseded_rows_retired"] == 0
