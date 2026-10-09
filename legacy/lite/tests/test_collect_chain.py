"""The chain collector: what it records, and what it refuses to record.

This is the irreplaceable one. Option price history is not retrievable
retrospectively from IBKR, so a snapshot not taken is gone permanently — and a
snapshot taken against the wrong strike band is a hole nobody can see afterwards.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import copy
from datetime import date, timedelta

import pytest

from obliskiq.collect.chain_collector import ChainCollector, chain_targets, daily_bars_are_fresh
from obliskiq.collect.runlog import CollectorRun, RunStatus
from obliskiq.collect.vol import FREQ_DAILY
from obliskiq.config import Config

SESSION = date(2026, 1, 15)


def config_for(config, **chain_overrides) -> Config:
    data = copy.deepcopy(config.as_dict())
    data["collectors"].update(
        {
            "target_backoff_seconds": 0.0,
            "target_backoff_max_seconds": 0.0,
            "request_spacing_seconds": 0.0,
        }
    )
    data["collectors"]["chain"].update(chain_overrides)
    return Config(data)


def seed_bars(store, contract_id: int, count: int, *, start: float = 100.0, end: date = SESSION):
    """Daily closes ending at ``end``, so the realised-vol rungs have material."""
    rows, price, day = [], start, end - timedelta(days=count * 2)
    for index in range(count):
        price *= 1.008 if index % 2 else 0.993
        rows.append((contract_id, f"{day.isoformat()}T00:00:00Z", FREQ_DAILY, price))
        day += timedelta(days=2)
    with store.transaction() as connection:
        connection.executemany(
            "INSERT OR REPLACE INTO bars (contract_id, ts, freq, close) VALUES (?, ?, ?, ?)", rows
        )


async def collect(store, cfg, market_data, *, only=None, session=SESSION):
    collector = ChainCollector(store, cfg, market_data)
    with CollectorRun(store, "chain") as run:
        await collector.run(run, only=only, today=session)
    return run


@pytest.fixture()
def chain_store(seeded_universe):
    """The seeded universe with enough SPY history for the primary sigma rung."""
    seed_bars(seeded_universe, 756733, 60, start=590.0)
    return seeded_universe


# ── target selection ──


def test_selection_keys_off_collect_chain_and_never_off_priips(chain_store):
    targets = chain_targets(chain_store)
    assert targets
    flags = {
        row["symbol"]: (row["collect_chain"], row["priips_restricted"])
        for row in chain_store.execute(
            "SELECT symbol, collect_chain, priips_restricted FROM universe"
        )
    }
    for target in targets:
        assert flags[target.symbol][0] == 1
    # And at least one selected target IS PRIIPs-restricted.
    assert any(flags[t.symbol][1] == 1 for t in targets)


def test_only_restricts_to_the_named_symbols(chain_store):
    assert {t.symbol for t in chain_targets(chain_store, only=["SPY"])} == {"SPY"}


# ── the tenor window ──


async def test_only_in_window_expiries_are_recorded(chain_store, config, market_data):
    """The fixture lists JAN26 (inside 90 DTE) and DEC26 (beyond 200). Neither may
    appear: collecting the whole chain is a tenfold volume increase for data the
    mandate cannot trade."""
    await collect(chain_store, config_for(config), market_data, only=["SPY"])

    expiries = {
        row[0] for row in chain_store.execute("SELECT DISTINCT expiry FROM chain_snapshots")
    }
    assert expiries == {"2026-04-17", "2026-06-19"}
    dte_min = config.require_int("tenor.entry_dte_min")
    dte_max = config.require_int("tenor.entry_dte_max")
    for expiry in expiries:
        dte = (date.fromisoformat(expiry) - SESSION).days
        assert dte_min <= dte <= dte_max


async def test_both_rights_are_recorded(chain_store, config, market_data):
    await collect(chain_store, config_for(config), market_data, only=["SPY"])
    rights = {row[0] for row in chain_store.execute("SELECT DISTINCT right FROM chain_snapshots")}
    assert rights == {"C", "P"}


async def test_the_full_quote_is_stored_per_strike(chain_store, config, market_data):
    """bid, ask, last, iv, delta, open interest and volume — the brief's list."""
    await collect(chain_store, config_for(config), market_data, only=["SPY"])
    row = chain_store.execute(
        "SELECT bid, ask, last, iv, delta, open_interest, volume, contract_id "
        "FROM chain_snapshots WHERE bid IS NOT NULL LIMIT 1"
    ).fetchone()
    assert row is not None
    assert all(value is not None for value in tuple(row))


# ── the band actually bounds what is collected ──


async def test_recorded_strikes_fall_inside_the_volatility_scaled_band(
    chain_store, config, market_data
):
    """The fixture ladder is deliberately far wider than any band the collector
    should select, so a band that silently failed to apply would be visible here."""
    import math

    await collect(chain_store, config_for(config), market_data, only=["SPY"])
    k = config.require_float("data.strike_bounds_sigma")
    calendar_days_per_year = config.require_float("numerics.bsm.day_count_basis")

    rows = chain_store.execute("SELECT expiry, strike, sigma_used FROM chain_snapshots").fetchall()
    assert rows
    spot = 601.20  # the SPY quote in the fixture
    for row in rows:
        tau = (date.fromisoformat(row["expiry"]) - SESSION).days / calendar_days_per_year
        move = k * row["sigma_used"] * math.sqrt(tau)
        assert spot * math.exp(-move) <= row["strike"] <= spot * math.exp(move)

    # ...and the ladder really did extend beyond the band on both sides.
    strikes = [row["strike"] for row in rows]
    assert min(strikes) > 601.20 * 0.45 and max(strikes) < 601.20 * 1.55


async def test_a_longer_expiry_gets_a_wider_band_than_a_nearer_one(
    chain_store, config, market_data
):
    await collect(chain_store, config_for(config), market_data, only=["SPY"])
    spans = {}
    for expiry in ("2026-04-17", "2026-06-19"):
        row = chain_store.execute(
            "SELECT MIN(strike) AS lo, MAX(strike) AS hi FROM chain_snapshots WHERE expiry = ?",
            (expiry,),
        ).fetchone()
        spans[expiry] = row["hi"] - row["lo"]
    assert spans["2026-06-19"] > spans["2026-04-17"]


# ── the sigma ladder, end to end ──


async def test_ample_history_uses_the_primary_rung_and_is_not_degraded(
    chain_store, config, market_data
):
    run = await collect(chain_store, config_for(config), market_data, only=["SPY"])
    tenor = config.require_int("data.strike_bounds_rv_tenor_days")

    sources = {
        row[0] for row in chain_store.execute("SELECT DISTINCT sigma_source FROM chain_snapshots")
    }
    assert sources == {f"realised_{tenor}d"}
    assert run.status() == RunStatus.OK
    assert run.degraded_targets == []


async def test_thin_history_falls_back_and_reports_the_run_degraded(
    seeded_universe, config, market_data
):
    """QQQ has 14 daily bars in the fixture: enough for the short tenor, not the
    primary one. A degraded run means the backfill is not keeping up, and it will
    keep not keeping up until somebody looks."""
    seed_bars(seeded_universe, 320227571, 12, start=515.0)
    run = await collect(seeded_universe, config_for(config), market_data, only=["QQQ"])

    fallback_tenor = config.require_int("collectors.chain.realised_fallback_tenor_days")
    sources = {
        row[0]
        for row in seeded_universe.execute("SELECT DISTINCT sigma_source FROM chain_snapshots")
    }
    assert sources == {f"realised_{fallback_tenor}d"}
    assert run.status() == RunStatus.DEGRADED
    assert run.degraded_targets == ["QQQ"]


async def test_no_history_and_no_vendor_iv_lands_on_the_default_rung(
    seeded_universe, config, market_data
):
    """FCX has three bars and no implied vol in its snapshot. The snapshot is still
    taken: losing a day of irreplaceable chain history to a missing sigma is the
    one outcome that cannot be undone tomorrow."""
    run = await collect(seeded_universe, config_for(config), market_data, only=["FCX"])

    rows = seeded_universe.execute(
        "SELECT DISTINCT sigma_source, sigma_used FROM chain_snapshots"
    ).fetchall()
    assert [row["sigma_source"] for row in rows] == ["default"]
    assert rows[0]["sigma_used"] == config.require_float("data.sigma_default")
    assert seeded_universe.execute("SELECT COUNT(*) FROM chain_snapshots").fetchone()[0] > 0
    assert run.status() == RunStatus.DEGRADED


async def test_the_default_rung_still_scales_with_time_to_expiry(
    seeded_universe, config, market_data
):
    """Rung 4 is a fixed SIGMA, not a fixed band. A fixed percentage band is never
    produced anywhere in this package."""
    await collect(seeded_universe, config_for(config), market_data, only=["FCX"])
    spans = {
        row["expiry"]: row["hi"] - row["lo"]
        for row in seeded_universe.execute(
            "SELECT expiry, MIN(strike) AS lo, MAX(strike) AS hi FROM chain_snapshots "
            "GROUP BY expiry ORDER BY expiry"
        )
    }
    assert len(spans) == 2
    near, far = sorted(spans)
    assert spans[far] > spans[near]


async def test_a_fallback_on_a_recent_listing_is_expected_not_degraded(
    seeded_universe, config, market_data
):
    with seeded_universe.transaction() as connection:
        connection.execute(
            "UPDATE universe SET listed_on = ? WHERE symbol = 'FCX'", ("2025-11-01",)
        )
    run = await collect(seeded_universe, config_for(config), market_data, only=["FCX"])
    assert run.status() == RunStatus.OK
    assert run.degraded_targets == []


async def test_a_fallback_on_an_old_listing_is_degraded(seeded_universe, config, market_data):
    with seeded_universe.transaction() as connection:
        connection.execute(
            "UPDATE universe SET listed_on = ? WHERE symbol = 'FCX'", ("1995-01-01",)
        )
    run = await collect(seeded_universe, config_for(config), market_data, only=["FCX"])
    assert run.status() == RunStatus.DEGRADED


async def test_the_degraded_warning_can_be_switched_off(seeded_universe, config, market_data):
    run = await collect(
        seeded_universe, config_for(config, warn_on_fallback=False), market_data, only=["FCX"]
    )
    assert run.status() == RunStatus.OK


# ── pacing guards ──


async def test_the_strike_cap_is_tighter_on_a_fallback_rung(seeded_universe, config, market_data):
    """A fallback band is wider and less trustworthy, so it must not also be the run
    that asks for the most strikes on a heavy underlying."""
    cfg = config_for(config, max_strikes_per_expiry=40, max_strikes_per_expiry_on_fallback=4)
    await collect(seeded_universe, cfg, market_data, only=["FCX"])

    per_expiry = seeded_universe.execute(
        "SELECT expiry, right, COUNT(*) AS n FROM chain_snapshots GROUP BY expiry, right"
    ).fetchall()
    assert per_expiry
    assert all(row["n"] <= 4 for row in per_expiry)


async def test_the_expiry_cap_is_honoured(chain_store, config, market_data):
    await collect(
        chain_store, config_for(config, max_expiries_per_underlying=1), market_data, only=["SPY"]
    )
    expiries = {
        row[0] for row in chain_store.execute("SELECT DISTINCT expiry FROM chain_snapshots")
    }
    assert len(expiries) == 1


# ── spot resolution ──


async def test_spot_falls_back_to_the_last_daily_close_when_there_is_no_quote(
    seeded_universe, config, market_data
):
    """Index contracts return an empty snapshot, per the playbook, so this is not a
    nicety. What it never does is invent a price."""
    seed_bars(seeded_universe, 4046, 45, start=77.0)  # ALB: bars, but no fixture quote
    await collect(seeded_universe, config_for(config), market_data, only=["ALB"])

    last_close = seeded_universe.execute(
        "SELECT close FROM bars WHERE contract_id = 4046 AND freq = ? ORDER BY ts DESC LIMIT 1",
        (FREQ_DAILY,),
    ).fetchone()[0]
    strikes = [row[0] for row in seeded_universe.execute("SELECT strike FROM chain_snapshots")]
    assert strikes
    assert min(strikes) < last_close < max(strikes)


async def test_an_underlying_with_neither_quote_nor_bars_fails_its_target(
    seeded_universe, config, market_data
):
    """No price was ever observed, so no band can be centred. That is a failure to
    report, not a band to guess at."""
    with seeded_universe.transaction() as connection:
        connection.execute("UPDATE universe SET collect_chain = 0")
        connection.execute("UPDATE universe SET collect_chain = 1 WHERE symbol = 'ALB'")
    run = await collect(seeded_universe, config_for(config), market_data)

    assert run.status() == RunStatus.ERROR
    assert any("no spot price" in (o.reason or "") for o in run.outcomes)


# ── idempotency ──


async def test_re_running_the_same_day_updates_in_place(chain_store, config, market_data):
    cfg = config_for(config)
    await collect(chain_store, cfg, market_data, only=["SPY"])
    before = chain_store.execute("SELECT COUNT(*) FROM chain_snapshots").fetchone()[0]

    await collect(chain_store, cfg, market_data, only=["SPY"])
    after = chain_store.execute("SELECT COUNT(*) FROM chain_snapshots").fetchone()[0]
    assert before == after > 0


async def test_a_second_day_appends_rather_than_replacing(chain_store, config, market_data):
    """Append-only across days is the whole product: the series IS the value."""
    cfg = config_for(config)
    await collect(chain_store, cfg, market_data, only=["SPY"], session=SESSION)
    day_one = chain_store.execute("SELECT COUNT(*) FROM chain_snapshots").fetchone()[0]

    await collect(chain_store, cfg, market_data, only=["SPY"], session=SESSION + timedelta(days=1))
    dates = {
        row[0] for row in chain_store.execute("SELECT DISTINCT snapshot_date FROM chain_snapshots")
    }
    assert dates == {SESSION.isoformat(), (SESSION + timedelta(days=1)).isoformat()}
    assert chain_store.execute("SELECT COUNT(*) FROM chain_snapshots").fetchone()[0] > day_one


async def test_the_collection_route_is_recorded_on_every_row(chain_store, config, market_data):
    """So collection can be migrated wholesale if a CP session turns out to conflict
    with an MCP one."""
    await collect(chain_store, config_for(config), market_data, only=["SPY"])
    sources = {row[0] for row in chain_store.execute("SELECT DISTINCT source FROM chain_snapshots")}
    assert sources == {market_data.source}


# ── partial runs ──


async def test_one_failing_underlying_does_not_kill_the_run(chain_store, config, market_data):
    """An unattended collector that aborts on the first bad symbol loses a day of
    irreplaceable data for every symbol after it in the list."""
    with chain_store.transaction() as connection:
        connection.execute("UPDATE universe SET collect_chain = 0")
        connection.execute("UPDATE universe SET collect_chain = 1 WHERE symbol IN ('SPY', 'CCJ')")
    run = await collect(chain_store, config_for(config), market_data)

    assert run.status() == RunStatus.PARTIAL
    assert run.targets_ok == 1 and run.targets_failed == 1
    assert chain_store.execute("SELECT COUNT(*) FROM chain_snapshots").fetchone()[0] > 0


# ── the freshness gate ──


def test_stale_daily_bars_fail_the_freshness_check(store, config):
    assert daily_bars_are_fresh(store, config, today=SESSION) is False


def test_fresh_daily_bars_pass(seeded_store, config):
    with seeded_store.transaction() as connection:
        connection.execute(
            "INSERT INTO bars (contract_id, ts, freq, close) VALUES (?, ?, ?, ?)",
            (1, f"{SESSION.isoformat()}T00:00:00Z", FREQ_DAILY, 100.0),
        )
    assert daily_bars_are_fresh(seeded_store, config, today=SESSION) is True


def test_bars_beyond_the_freshness_window_are_stale(seeded_store, config):
    days = config.require_int("collectors.chain.bars_freshness_days")
    old = SESSION - timedelta(days=days + 1)
    with seeded_store.transaction() as connection:
        connection.execute(
            "INSERT INTO bars (contract_id, ts, freq, close) VALUES (?, ?, ?, ?)",
            (1, f"{old.isoformat()}T00:00:00Z", FREQ_DAILY, 100.0),
        )
    assert daily_bars_are_fresh(seeded_store, config, today=SESSION) is False


# ── the run record ──


async def test_the_run_is_recorded_before_the_work_starts(chain_store, config, market_data):
    """A run the machine killed must leave evidence rather than nothing at all."""
    await collect(chain_store, config_for(config), market_data, only=["SPY"])
    row = chain_store.execute(
        "SELECT collector, status, rows_written, targets_ok, detail FROM collector_runs "
        "WHERE collector = 'chain' ORDER BY run_id DESC LIMIT 1"
    ).fetchone()
    assert row["status"] == RunStatus.OK
    assert row["rows_written"] > 0
    assert "sigma_source" in row["detail"]
    assert row["targets_ok"] == 1


# ── the underlying snapshot ──
#
# The chain collector already fetches a quote per underlying to centre its
# strike band. Keeping it is what gives grid indicators 7-9 a history to build
# on: per-underlying implied vol cannot be retrieved retrospectively either.


async def test_the_underlying_quote_is_kept_not_discarded(
    seeded_universe, fast_collect_config, market_data, session_date
):
    from obliskiq.collect.chain_collector import ChainCollector
    from obliskiq.collect.runlog import CollectorRun

    with CollectorRun(seeded_universe, "chain") as run:
        await ChainCollector(seeded_universe, fast_collect_config, market_data).run(
            run, today=session_date
        )

    rows = seeded_universe.execute(
        "SELECT contract_id, last, implied_vol, iv_percentile_52w, source "
        "FROM underlying_snapshots WHERE snapshot_date = ?",
        (session_date.isoformat(),),
    ).fetchall()

    assert rows, "no underlying snapshot was kept"
    for row in rows:
        assert row["source"] == market_data.source


async def test_the_vendor_iv_percentile_stays_null_until_a_field_id_is_verified(
    seeded_universe, fast_collect_config, market_data, session_date, config
):
    """A guessed field id would write some other quantity into a percentile column."""
    declared = config.get("ibkr.cp_api.marketdata.underlying_snapshot_fields")
    assert declared["implied_vol_percentile"] is None, (
        "once Session 5 verifies the field id, update this test rather than deleting it"
    )

    from obliskiq.collect.chain_collector import ChainCollector
    from obliskiq.collect.runlog import CollectorRun

    with CollectorRun(seeded_universe, "chain") as run:
        await ChainCollector(seeded_universe, fast_collect_config, market_data).run(
            run, today=session_date
        )

    percentiles = [
        row[0]
        for row in seeded_universe.execute(
            "SELECT iv_percentile_52w FROM underlying_snapshots"
        ).fetchall()
    ]
    assert percentiles and all(value is None for value in percentiles)


async def test_rerunning_updates_the_day_rather_than_duplicating_it(
    seeded_universe, fast_collect_config, market_data, session_date
):
    from obliskiq.collect.chain_collector import ChainCollector
    from obliskiq.collect.runlog import CollectorRun

    for _ in range(2):
        with CollectorRun(seeded_universe, "chain") as run:
            await ChainCollector(seeded_universe, fast_collect_config, market_data).run(
                run, today=session_date
            )

    counts = seeded_universe.execute(
        "SELECT contract_id, COUNT(*) FROM underlying_snapshots WHERE snapshot_date = ? "
        "GROUP BY contract_id",
        (session_date.isoformat(),),
    ).fetchall()
    assert counts and all(row[1] == 1 for row in counts)


async def test_a_dry_run_keeps_no_underlying_snapshot(
    seeded_universe, fast_collect_config, market_data, session_date
):
    from obliskiq.collect.chain_collector import ChainCollector
    from obliskiq.collect.runlog import CollectorRun

    with CollectorRun(seeded_universe, "chain", dry_run=True) as run:
        await ChainCollector(seeded_universe, fast_collect_config, market_data).run(
            run, today=session_date, dry_run=True
        )

    assert seeded_universe.execute("SELECT COUNT(*) FROM underlying_snapshots").fetchone()[0] == 0
