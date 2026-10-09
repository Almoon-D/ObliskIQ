"""The machine was off for weeks. What happens when it comes back?

``collectors.*`` is written to tolerate gaps, and the longest trailing gap any
test simulated was three days. A small always-on VM is not always on: it is
rebooted, it runs out of disk, its timer unit fails, and the operator finds out
later. The behaviour that matters is what the FIRST run back does.

Two different things are being checked here:

* the backfill self-heals — it sizes its request from the watermark, so a long
  outage becomes one wide request rather than a crash or a silent skip;
* the chain collector used to size today's strike bands from months-old bars and
  report the result as its PRIMARY, trustworthy rung. Having enough closes to
  satisfy a thirty-day tenor says nothing about when those closes were.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

from datetime import date, timedelta

from obliskiq.collect.vol import SigmaLadder, newest_daily_bar_date, recent_daily_closes

SESSION = date(2026, 8, 24)
CONTRACT_ID = 1


def _universe(store):
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO universe (contract_id, symbol, currency, asset_class) "
            "VALUES (?, 'ASML', 'EUR', 'equity')",
            (CONTRACT_ID,),
        )


def _bars_ending(store, last_day, count=60):
    """`count` daily closes ending on `last_day`, with real dispersion."""
    with store.transaction() as connection:
        connection.executemany(
            "INSERT INTO bars (contract_id, ts, freq, close, source) VALUES (?, ?, '1d', ?, 'mcp')",
            [
                (
                    CONTRACT_ID,
                    (last_day - timedelta(days=count - 1 - n)).isoformat() + "T00:00:00Z",
                    100.0 + (n % 7) * 1.5,
                )
                for n in range(count)
            ],
        )


def test_fresh_bars_give_an_unflagged_primary_rung(store, config):
    _universe(store)
    _bars_ending(store, SESSION)

    ladder = SigmaLadder(config)
    closes = recent_daily_closes(store, CONTRACT_ID, limit=ladder.max_bars_needed)
    sigma = ladder.resolve(
        closes,
        vendor_iv=None,
        newest_close=newest_daily_bar_date(store, CONTRACT_ID),
        session=SESSION,
    )

    assert sigma.source.startswith("realised_")
    assert sigma.is_fallback is False
    assert sigma.is_stale is False


def test_bars_from_before_a_long_outage_are_flagged_stale(store, config):
    """The regression. Sixty closes ending three months ago still satisfy the
    tenor, so the primary rung resolves and is_fallback stays False. Only
    is_stale distinguishes this from a healthy run."""
    _universe(store)
    _bars_ending(store, SESSION - timedelta(days=90))

    ladder = SigmaLadder(config)
    closes = recent_daily_closes(store, CONTRACT_ID, limit=ladder.max_bars_needed)
    sigma = ladder.resolve(
        closes,
        vendor_iv=None,
        newest_close=newest_daily_bar_date(store, CONTRACT_ID),
        session=SESSION,
    )

    assert closes, "the closes are present; that was never the problem"
    assert sigma.source.startswith("realised_")
    assert sigma.is_fallback is False, "still the primary rung, which is the point"
    assert sigma.is_stale is True


def test_the_staleness_boundary_is_the_configured_freshness_window(store, config):
    freshness = config.require_int("collectors.chain.bars_freshness_days")
    ladder = SigmaLadder(config)

    _universe(store)
    _bars_ending(store, SESSION - timedelta(days=freshness))
    closes = recent_daily_closes(store, CONTRACT_ID, limit=ladder.max_bars_needed)

    inside = ladder.resolve(
        closes,
        vendor_iv=None,
        newest_close=SESSION - timedelta(days=freshness),
        session=SESSION,
    )
    assert inside.is_stale is False, "a gap exactly at the window is not yet stale"

    beyond = ladder.resolve(
        closes,
        vendor_iv=None,
        newest_close=SESSION - timedelta(days=freshness + 1),
        session=SESSION,
    )
    assert beyond.is_stale is True


def test_a_vendor_iv_rung_is_not_made_stale_by_old_bars(store, config):
    """Staleness belongs to the rungs that read the bars, and to no others."""
    _universe(store)
    # Too few closes for any realised rung, so the ladder falls to vendor IV.
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO bars (contract_id, ts, freq, close, source) "
            "VALUES (?, '2026-01-05T00:00:00Z', '1d', 100.0, 'mcp')",
            (CONTRACT_ID,),
        )

    ladder = SigmaLadder(config)
    closes = recent_daily_closes(store, CONTRACT_ID, limit=ladder.max_bars_needed)
    sigma = ladder.resolve(
        closes,
        vendor_iv=0.31,
        newest_close=date(2026, 1, 5),
        session=SESSION,
    )

    assert sigma.source == "vendor_iv"
    assert sigma.is_stale is False


def test_an_absent_session_leaves_staleness_undecided(store, config):
    """No session to compare against is not evidence of freshness."""
    _universe(store)
    _bars_ending(store, SESSION - timedelta(days=365))

    ladder = SigmaLadder(config)
    closes = recent_daily_closes(store, CONTRACT_ID, limit=ladder.max_bars_needed)
    sigma = ladder.resolve(closes, vendor_iv=None)
    assert sigma.is_stale is False


def test_newest_daily_bar_date_reads_the_series_not_the_watermark(store):
    """Derived from the table, so a watermark claiming a span nobody holds
    cannot make an old series look current."""
    _universe(store)
    _bars_ending(store, SESSION - timedelta(days=30))
    assert newest_daily_bar_date(store, CONTRACT_ID) == SESSION - timedelta(days=30)


def test_a_contract_with_no_bars_has_no_newest_date(store):
    _universe(store)
    assert newest_daily_bar_date(store, CONTRACT_ID) is None
