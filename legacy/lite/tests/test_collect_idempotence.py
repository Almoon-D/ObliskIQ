"""Re-running a day spends nothing and writes nothing new, and a crash mid-write
does not make a day look collected.

Two properties an unattended collector needs and neither of which was proved:

* **Idempotence under retry.** A systemd unit with ``Restart=on-failure`` re-runs
  the sequence. A day already collected must cost no vendor credit and add no
  row, or a transient failure at the end of a long run becomes a bill and a
  duplicate.
* **Atomicity of the pair that decides whether a day gets re-bought.** The vendor
  fetch log used to be committed ``ok`` in one transaction and the rows written
  in the next. Between them the process is killable, and ``_already_held``
  refuses to re-buy a settled past session logged ``ok`` — so a kill in that
  window bought a day, wrote nothing, and marked it held forever.

The first is proved by counting calls and rows, not by reading the code, which is
what the objective asks for.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

from datetime import date

import httpx
import pytest
import respx

from obliskiq.collect.option_vendor import VendorChainCollector, VendorClient
from obliskiq.collect.runlog import CollectorRun
from obliskiq.errors import StoreError

# tests/ is not a package; test_rates.py imports from conftest the same way.
from test_collect_option_vendor import (
    HTTP_CACHED,
    SESSION,
    VENDOR_CHAIN_URL,
    _vendor_config,
    load,
)

COUNTED_TABLES = (
    "chain_snapshots",
    "underlying_snapshots",
    "vendor_fetch_log",
)


def _counts(store, tables=COUNTED_TABLES):
    return {table: store.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in tables}


def _chain_route(*, credits="1"):
    return respx.get(url__startswith=VENDOR_CHAIN_URL).mock(
        return_value=httpx.Response(
            HTTP_CACHED,
            json=load("chain_SPY_live"),
            headers={"x-api-ratelimit-consumed": credits},
        )
    )


async def _vendor_run(store, cfg, *, today=SESSION, force=False, write=None):
    """One vendor-chain run. ``write`` replaces the collector's write step."""
    async with httpx.AsyncClient() as http:
        collector = VendorChainCollector(store, cfg, VendorClient(cfg, http, "tok"))
        if write is not None:
            collector._write = write
        with CollectorRun(store, "vendor-chain") as run:
            await collector.run(run, only=["SPY"], sessions=1, today=today, force=force)
        return run, collector


# ── idempotence ──


@respx.mock
async def test_a_second_vendor_run_over_a_held_day_makes_no_request_at_all(seeded_universe, config):
    """Zero calls, not merely zero rows: the credit is spent by the REQUEST, so a
    check that only counted rows would pass while the bill grew."""
    route = _chain_route()
    cfg = _vendor_config(config)

    await _vendor_run(seeded_universe, cfg)
    assert route.call_count == 1
    after_first = _counts(seeded_universe)

    _, collector = await _vendor_run(seeded_universe, cfg)
    assert route.call_count == 1, "the second run asked the vendor nothing"
    assert collector.credits_spent == 0
    assert _counts(seeded_universe) == after_first


@respx.mock
async def test_force_does_re_buy_so_the_previous_test_is_not_vacuous(seeded_universe, config):
    """The control. Without it, a collector that had simply stopped working would
    pass the idempotence test perfectly."""
    route = _chain_route()
    cfg = _vendor_config(config)

    await _vendor_run(seeded_universe, cfg)
    await _vendor_run(seeded_universe, cfg, force=True)
    assert route.call_count == 2


@respx.mock
async def test_re_buying_the_same_day_updates_rather_than_duplicating(seeded_universe, config):
    """--force re-buys, and the primary key means it overwrites. Append-only
    across days, idempotent within one."""
    _chain_route()
    cfg = _vendor_config(config)

    await _vendor_run(seeded_universe, cfg)
    before = _counts(seeded_universe)
    await _vendor_run(seeded_universe, cfg, force=True)
    assert _counts(seeded_universe) == before


async def test_re_running_a_collected_day_adds_no_bars_or_chain_rows(
    seeded_universe, fast_collect_config, market_data
):
    """The IBKR route has no fetch log — it re-walks the day — so what has to hold
    is that re-walking writes over rather than beside."""
    from obliskiq.collect.backfill import Backfill
    from obliskiq.collect.chain_collector import ChainCollector

    session = date(2026, 1, 15)
    tables = ("bars", "bar_watermarks", "chain_snapshots", "underlying_snapshots")

    async def once():
        with CollectorRun(seeded_universe, "backfill") as run:
            await Backfill(seeded_universe, fast_collect_config, market_data).run(run, only=["SPY"])
        with CollectorRun(seeded_universe, "chain") as run:
            await ChainCollector(seeded_universe, fast_collect_config, market_data).run(
                run, only=["SPY"], today=session
            )

    await once()
    first = _counts(seeded_universe, tables)
    assert first["chain_snapshots"] > 0, "the run collected something to be idempotent about"

    await once()
    assert _counts(seeded_universe, tables) == first


# ── atomicity of the pair that decides a re-buy ──


@respx.mock
async def test_a_write_that_fails_does_not_leave_the_day_marked_held(seeded_universe, config):
    """The crash window. The fetch log used to be committed 'ok' before the rows
    were written, so a kill in between bought the day, wrote nothing, and
    suppressed every future attempt at it."""
    route = _chain_route()
    cfg = _vendor_config(config)

    def explode(target, chain):
        raise StoreError("disk full")

    run, _ = await _vendor_run(seeded_universe, cfg, write=explode)
    assert run.targets_failed == 1

    held = seeded_universe.execute(
        "SELECT status, credits FROM vendor_fetch_log WHERE symbol = 'SPY'"
    ).fetchone()
    assert held is not None, "the credit spent is still recorded; it was really spent"
    assert held["status"] == "error", "and an error is retryable, which is the point"
    assert held["credits"] == 1, "what it cost is not forgotten just because it failed"
    assert seeded_universe.execute("SELECT COUNT(*) FROM chain_snapshots").fetchone()[0] == 0

    # And the next run does go back for it.
    await _vendor_run(seeded_universe, cfg)
    assert route.call_count == 2
    assert seeded_universe.execute("SELECT COUNT(*) FROM chain_snapshots").fetchone()[0] == 3


@respx.mock
async def test_an_ok_fetch_log_row_implies_the_rows_are_there(seeded_universe, config):
    """The invariant the atomicity buys, stated as an invariant."""
    _chain_route()
    await _vendor_run(seeded_universe, _vendor_config(config))

    for row in seeded_universe.execute(
        "SELECT symbol, trade_date, contracts FROM vendor_fetch_log WHERE status = 'ok'"
    ).fetchall():
        stored = seeded_universe.execute(
            "SELECT COUNT(*) FROM chain_snapshots WHERE snapshot_date = ?",
            (row["trade_date"],),
        ).fetchone()[0]
        assert stored > 0, f"{row['symbol']} on {row['trade_date']} is logged ok with no rows"


async def test_a_chain_underlying_that_fails_leaves_no_snapshot_behind(
    seeded_universe, fast_collect_config, market_data, monkeypatch
):
    """underlying_snapshots used to be committed at the top of the underlying and
    the chain rows at the bottom. A failure in between left a snapshot with no
    chain for that date — which reads exactly like a chain that returned nothing,
    and underlying_snapshots.implied_vol is the series the IV percentile is
    built from."""
    from obliskiq.collect import chain_collector as module

    session = date(2026, 1, 15)
    collector = module.ChainCollector(seeded_universe, fast_collect_config, market_data)

    async def no_months(symbol):
        raise RuntimeError("the exchange dropped the connection mid-walk")

    monkeypatch.setattr(collector._market_data, "option_months", no_months)

    with CollectorRun(seeded_universe, "chain") as run:
        await collector.run(run, only=["SPY"], today=session)

    assert run.targets_failed == 1
    assert seeded_universe.execute("SELECT COUNT(*) FROM underlying_snapshots").fetchone()[0] == 0
    assert seeded_universe.execute("SELECT COUNT(*) FROM chain_snapshots").fetchone()[0] == 0


@pytest.mark.parametrize("table", ["chain_snapshots", "underlying_snapshots"])
async def test_a_successful_chain_underlying_writes_both_halves(
    seeded_universe, fast_collect_config, market_data, table
):
    """The other side of the same coin: making the pair atomic must not have made
    either half optional."""
    from obliskiq.collect.chain_collector import ChainCollector

    with CollectorRun(seeded_universe, "chain") as run:
        await ChainCollector(seeded_universe, fast_collect_config, market_data).run(
            run, only=["SPY"], today=date(2026, 1, 15)
        )
    assert run.targets_ok == 1
    assert seeded_universe.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] > 0
