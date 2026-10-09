"""The screen, and the guarantee that it and the census share one gate path.

The census counts how many contracts clear every gate at once; the screener
returns which ones. If those two ever ran different evaluations, the weekly
viability number would be measuring something the screener does not enforce —
and a threshold could be quietly loosened on one side without the other
noticing. The delegation is asserted here rather than left as a convention.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from obliskiq.compute.screen import (
    BUCKET_CHEAP,
    BUCKET_COILED,
    BUCKET_EXPENSIVE,
    BUCKET_EXTENDED,
    BUCKET_UNCLASSIFIED,
    OptionScreen,
)
from obliskiq.compute.universe_census import UniverseCensus

AS_OF = date(2026, 1, 15)
SNAPSHOT = AS_OF
NAV = 200_000.0

#: Inside tenor.entry_dte_min..entry_dte_max.
LIVE_EXPIRY = AS_OF + timedelta(days=150)


@pytest.fixture()
def screen(seeded_store, config):
    return OptionScreen(seeded_store, config)


def _chain(store, rows, *, snapshot=SNAPSHOT, underlying_id=1):
    with store.transaction() as connection:
        connection.executemany(
            "INSERT INTO chain_snapshots (snapshot_date, underlying_id, expiry, strike, "
            "right, bid, ask, last, iv, delta, open_interest, volume, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'cp_api')",
            [
                (
                    snapshot.isoformat(),
                    underlying_id,
                    expiry.isoformat(),
                    strike,
                    right,
                    bid,
                    ask,
                    (bid + ask) / 2,
                    iv,
                    delta,
                    open_interest,
                    volume,
                )
                for expiry, strike, right, bid, ask, iv, delta, open_interest, volume in rows
            ],
        )


def _snapshot(store, *, iv_percentile, hv=0.40, contract_id=1, day=SNAPSHOT):
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO underlying_snapshots (snapshot_date, contract_id, last, "
            "implied_vol, iv_percentile_52w, historical_vol_30d, source) "
            "VALUES (?, ?, ?, ?, ?, ?, 'cp_api')",
            (day.isoformat(), contract_id, 100.0, 0.30, iv_percentile, hv),
        )


def _nav(store, *, nav=NAV):
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO nav_history (as_of, nav_base, source, recorded_at) VALUES (?, ?, ?, ?)",
            (AS_OF.isoformat(), nav, "ibkr", "2026-01-15T00:00:00Z"),
        )


def _liquid(strike=100.0, *, expiry=LIVE_EXPIRY, bid=2.40, ask=2.60, oi=10_000, volume=500):
    return (expiry, strike, "C", bid, ask, 0.35, 0.40, oi, volume)


# ── the shared evaluation ──


def test_the_census_and_the_screen_agree_on_the_survivor_count(seeded_store, config):
    """One evaluation, two questions. A drift here is a threshold enforced twice."""
    _nav(seeded_store)
    _snapshot(seeded_store, iv_percentile=0.10)
    _chain(
        seeded_store,
        [
            _liquid(100.0),
            _liquid(105.0),
            # Fails liquidity: a spread nobody can cross.
            (LIVE_EXPIRY, 110.0, "C", 0.10, 2.00, 0.35, 0.20, 12_000, 400),
            # Fails tenor: inside the minimum entry window.
            _liquid(115.0, expiry=AS_OF + timedelta(days=30)),
        ],
    )

    survivors = OptionScreen(seeded_store, config).screen(as_of=AS_OF).survivors
    census = UniverseCensus(seeded_store, config).run(as_of=AS_OF)

    assert census.passed_all_gates == len(survivors)
    assert census.contracts_scanned == 4


def test_the_census_delegates_rather_than_rebuilding_the_gates(seeded_store, config, monkeypatch):
    """Asserted structurally: a copied evaluation would pass the count test too."""
    census = UniverseCensus(seeded_store, config)
    calls: list[int] = []
    original = census._screen.evaluate

    def counted(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(census._screen, "evaluate", counted)
    _nav(seeded_store)
    _chain(seeded_store, [_liquid(100.0), _liquid(105.0)])
    census.run(as_of=AS_OF)
    assert len(calls) == 2


# ── the budget comes first ──


def test_the_screen_refuses_without_a_nav(seeded_store, config):
    """The budget defines the universe; screening without it is the failure mode."""
    _chain(seeded_store, [_liquid()])
    result = OptionScreen(seeded_store, config).screen(as_of=AS_OF)
    assert result.survivors == ()
    assert "budget defines the universe" in (result.reason or "")


def test_a_premium_above_the_ceiling_never_survives(seeded_store, config):
    _nav(seeded_store, nav=20_000.0)
    _snapshot(seeded_store, iv_percentile=0.10)
    _chain(seeded_store, [_liquid(bid=900.0, ask=910.0)])
    assert OptionScreen(seeded_store, config).screen(as_of=AS_OF).survivors == ()


def test_a_crossed_quote_never_survives_the_liquidity_gate(seeded_store, config):
    """ask < bid makes spread_pct_of_mid negative, which cleared the liquidity
    gate's `spread > max_spread_pct` ceiling — a crossed book read as tighter
    than any real quote on the chain and passed straight through to the
    dislocation screen."""
    _nav(seeded_store)
    _snapshot(seeded_store, iv_percentile=0.05, hv=0.50)
    _chain(seeded_store, [_liquid(bid=2.60, ask=2.40)])  # crossed: ask < bid
    assert OptionScreen(seeded_store, config).screen(as_of=AS_OF).survivors == ()


# ── buckets ──


def test_cheap_is_both_conditions_the_way_skill_md_defines_it(screen, seeded_store):
    """IV materially below realised AND an IV percentile at or below the ceiling.

    The two labels were inverted: a strike meeting both came back `coiled`, and
    one meeting only the percentile came back `cheap` — so the screen's headline
    bucket named the case where implied vol is NOT below realised.
    """
    _nav(seeded_store)
    _snapshot(seeded_store, iv_percentile=0.05, hv=0.50)  # IV 0.30 < HV 0.50
    _chain(seeded_store, [_liquid()])
    survivors = screen.screen(as_of=AS_OF).survivors
    assert [c.bucket for c in survivors] == [BUCKET_CHEAP]


def test_a_low_percentile_with_iv_above_realised_is_not_cheap(screen, seeded_store):
    """Half the test is not the test. This is the case that used to read `cheap`."""
    _nav(seeded_store)
    _snapshot(seeded_store, iv_percentile=0.05, hv=0.20)  # IV 0.30 > HV 0.20
    _chain(seeded_store, [_liquid()])
    survivors = screen.screen(as_of=AS_OF).survivors
    assert [c.bucket for c in survivors] == [BUCKET_EXPENSIVE]


def test_coiled_needs_the_underlyings_own_state_and_never_assumes_it(screen):
    """Coiled is cheap PLUS compressed realised vol and a price near the low.

    Those are properties of the underlying, not of a strike. An unmeasured
    condition leaves the row cheap rather than promoting it.
    """
    assert screen.bucket(0.05, iv_below_hv=True) == BUCKET_CHEAP
    assert (
        screen.bucket(0.05, iv_below_hv=True, rv_compressed=None, near_the_low=True) == BUCKET_CHEAP
    )
    assert (
        screen.bucket(0.05, iv_below_hv=True, rv_compressed=True, near_the_low=False)
        == BUCKET_CHEAP
    )
    assert (
        screen.bucket(0.05, iv_below_hv=True, rv_compressed=True, near_the_low=True)
        == BUCKET_COILED
    )


def test_a_high_iv_percentile_is_extended(screen, seeded_store, config):
    """Extended contracts fail the cheap-IV gate, so they surface as rejections."""
    assert screen.bucket(0.95, iv_below_hv=False) == BUCKET_EXTENDED
    assert screen.bucket(0.95, iv_below_hv=True) == BUCKET_EXTENDED


def test_an_unserved_iv_percentile_is_unclassified_not_ordinary(screen):
    """An untested gate is not a passed gate, and neither is an unread percentile."""
    assert screen.bucket(None, iv_below_hv=False) == BUCKET_UNCLASSIFIED


def test_a_contract_with_an_unserved_percentile_is_reported_separately(screen, seeded_store):
    _nav(seeded_store)
    _chain(seeded_store, [_liquid()])  # no underlying snapshot: the gate is untested
    result = screen.screen(as_of=AS_OF)
    assert result.survivors == ()
    assert len(result.incomplete) == 1
    assert result.incomplete[0].bucket == BUCKET_UNCLASSIFIED


# ── ranking and bounds ──


def test_survivors_rank_cheapest_volatility_first(screen, seeded_store):
    """The screen exists to find implied vol BELOW realised, which is negative.

    It sorted on the signed spread descending, so the most expensive contract
    came first and `limit` truncated away exactly the tail being looked for.
    """
    _nav(seeded_store)
    _snapshot(seeded_store, iv_percentile=0.10, hv=0.40)
    _chain(
        seeded_store,
        [
            # IV 0.30 against HV 0.40: cheap, spread -0.10.
            (LIVE_EXPIRY, 100.0, "C", 2.40, 2.60, 0.30, 0.4, 10_000, 500),
            # IV 0.60 against the same HV: expensive, spread +0.20.
            (LIVE_EXPIRY, 105.0, "C", 2.40, 2.60, 0.60, 0.4, 10_000, 500),
        ],
    )
    spreads = [c.iv_minus_hv for c in screen.screen(as_of=AS_OF).survivors]
    assert spreads == sorted(spreads), "cheapest volatility has to come first"
    assert spreads[0] < 0 < spreads[-1], "the fixture no longer spans both signs"


def test_a_limit_keeps_the_cheap_tail_rather_than_truncating_it(screen, seeded_store):
    """The consequence of the ordering, stated as the behaviour that matters."""
    _nav(seeded_store)
    _snapshot(seeded_store, iv_percentile=0.10, hv=0.40)
    _chain(
        seeded_store,
        [
            (LIVE_EXPIRY, 100.0, "C", 2.40, 2.60, 0.20, 0.4, 10_000, 500),
            (LIVE_EXPIRY, 105.0, "C", 2.40, 2.60, 0.55, 0.4, 10_000, 500),
            (LIVE_EXPIRY, 110.0, "C", 2.40, 2.60, 0.60, 0.4, 10_000, 500),
        ],
    )
    kept = screen.screen(as_of=AS_OF, limit=1).survivors
    assert len(kept) == 1
    assert kept[0].iv == pytest.approx(0.20), "the limit kept the cheapest, not the dearest"


def test_a_candidate_with_no_computable_spread_sorts_last(screen, seeded_store):
    """An absent number is not a cheap one."""
    _nav(seeded_store)
    _snapshot(seeded_store, iv_percentile=0.10, hv=0.20)
    _chain(
        seeded_store,
        [
            (LIVE_EXPIRY, 100.0, "C", 2.40, 2.60, None, 0.4, 10_000, 500),
            (LIVE_EXPIRY, 105.0, "C", 2.40, 2.60, 0.60, 0.4, 10_000, 500),
        ],
    )
    survivors = screen.screen(as_of=AS_OF).survivors
    assert survivors[-1].iv_minus_hv is None


def test_a_limit_truncates_and_says_so(screen, seeded_store):
    _nav(seeded_store)
    _snapshot(seeded_store, iv_percentile=0.10)
    _chain(seeded_store, [_liquid(100.0 + index) for index in range(5)])
    result = screen.screen(as_of=AS_OF, limit=2)
    assert len(result.survivors) == 2
    assert result.truncated


def test_a_symbol_filter_narrows_the_scan(screen, seeded_store):
    _nav(seeded_store)
    _snapshot(seeded_store, iv_percentile=0.10)
    _chain(seeded_store, [_liquid()])
    assert screen.screen(as_of=AS_OF, symbols=["TEST"]).contracts_scanned == 1
    assert screen.screen(as_of=AS_OF, symbols=["NOTHING"]).contracts_scanned == 0


# ── the spread is measured against SELF-computed realised vol ──


def _daily_closes(store, closes, *, contract_id=1, end=SNAPSHOT):
    """Stored daily bars, ending on the snapshot date."""
    with store.transaction() as connection:
        connection.executemany(
            "INSERT INTO bars (contract_id, ts, freq, open, high, low, close, volume, source) "
            "VALUES (?, ?, '1d', ?, ?, ?, ?, 1000, 'cp_api')",
            [
                (
                    contract_id,
                    (end - timedelta(days=len(closes) - 1 - i)).isoformat() + "T00:00:00Z",
                    close,
                    close,
                    close,
                    close,
                )
                for i, close in enumerate(closes)
            ],
        )


def _alternating(n, *, start=100.0, step=0.02):
    """Closes with a known, non-zero dispersion the vendor figure will not match."""
    out = [start]
    for i in range(n):
        out.append(out[-1] * (1 + step if i % 2 == 0 else 1 - step))
    return out


def test_the_spread_uses_self_computed_realised_vol_not_the_vendors(seeded_store, config, screen):
    """The defect this test exists for, measured live on ASML 2026-08-24.

    IBKR's historical-vol.annual_pct read 0.5399 where the same daily closes give
    0.4623 — 16.8% apart. Against an ATM implied of 0.4604 that is a spread of
    -0.0794 or -0.0019, a factor of 42, and the difference between "cheap
    volatility, rank it top" and "fairly priced, ignore it". The grid's indicator
    9 was already self-computed; the screen was not, so the two disagreed about
    the same instrument on the same day.
    """
    _nav(seeded_store)
    # A vendor figure deliberately far from anything these closes imply.
    _snapshot(seeded_store, iv_percentile=0.10, hv=0.90)
    _daily_closes(seeded_store, _alternating(40))
    _chain(seeded_store, [_liquid()])

    result = screen.screen(as_of=AS_OF)
    candidate = (result.survivors + result.incomplete)[0]

    assert candidate.hv_source == "self"
    assert candidate.historical_vol_30d == pytest.approx(0.90), "the vendor figure is still kept"
    assert candidate.realised_vol_self is not None
    assert candidate.realised_vol_self != pytest.approx(0.90)
    # The spread is against the self-computed figure, not the vendor's.
    assert candidate.iv_minus_hv == pytest.approx(candidate.iv - candidate.realised_vol_self)
    assert candidate.iv_minus_hv != pytest.approx(candidate.iv - 0.90)


def test_the_screen_and_the_grid_measure_realised_vol_with_one_function(
    seeded_store, config, screen
):
    """Two implementations of "realised vol" would be two rankings.

    Asserted against the collectors' own function — the same one the chain
    collector sizes its strike bands with and indicators.iv_hv_spread measures
    against — rather than against a number recomputed here, which would only
    prove this test agrees with itself.
    """
    from obliskiq.collect.vol import realised_vol

    _nav(seeded_store)
    _snapshot(seeded_store, iv_percentile=0.10, hv=0.90)
    closes = _alternating(40)
    _daily_closes(seeded_store, closes)
    _chain(seeded_store, [_liquid()])

    candidate = (screen.screen(as_of=AS_OF).survivors + screen.screen(as_of=AS_OF).incomplete)[0]
    expected = realised_vol(
        closes,
        tenor_days=config.require_int("data.strike_bounds_rv_tenor_days"),
        trading_days_per_year=config.require_float("data.trading_days_per_year"),
    )
    assert candidate.realised_vol_self == pytest.approx(expected)


def test_too_few_closes_falls_back_to_the_vendor_and_says_so(seeded_store, config, screen):
    """A fallback is legitimate; a SILENT one is not. hv_source carries which."""
    _nav(seeded_store)
    _snapshot(seeded_store, iv_percentile=0.10, hv=0.42)
    _daily_closes(seeded_store, [100.0, 101.0, 100.5])  # nowhere near the tenor
    _chain(seeded_store, [_liquid()])

    candidate = (screen.screen(as_of=AS_OF).survivors + screen.screen(as_of=AS_OF).incomplete)[0]

    assert candidate.realised_vol_self is None
    assert candidate.hv_source == "vendor"
    assert candidate.iv_minus_hv == pytest.approx(candidate.iv - 0.42)


def test_no_realised_vol_from_either_source_is_no_spread(seeded_store, config, screen):
    """Never a spread against a realised vol that was not measured."""
    _nav(seeded_store)
    _snapshot(seeded_store, iv_percentile=0.10, hv=None)
    _chain(seeded_store, [_liquid()])

    candidate = (screen.screen(as_of=AS_OF).survivors + screen.screen(as_of=AS_OF).incomplete)[0]
    assert candidate.iv_minus_hv is None
    assert candidate.hv_source is None


def test_realised_vol_is_computed_once_per_underlying_not_once_per_strike(
    seeded_store, config, screen, monkeypatch
):
    """A chain contributes one candidate per surviving strike, all asking the same
    question of the same instrument."""
    _nav(seeded_store)
    _snapshot(seeded_store, iv_percentile=0.10, hv=0.42)
    _daily_closes(seeded_store, _alternating(40))
    _chain(seeded_store, [_liquid(strike) for strike in (95.0, 100.0, 105.0, 110.0)])

    from obliskiq.compute import screen as screen_module

    calls: list[int] = []
    original = screen_module.recent_daily_closes

    def counted(store, contract_id, *, limit, until=None):
        calls.append(contract_id)
        return original(store, contract_id, limit=limit, until=until)

    monkeypatch.setattr(screen_module, "recent_daily_closes", counted)

    result = screen.screen(as_of=AS_OF)
    assert len(result.survivors + result.incomplete) > 1, "need several strikes to be a test"
    assert calls == [1], f"one lookup expected, got {len(calls)}"


def test_the_cache_does_not_survive_into_the_next_run(seeded_store, config, screen):
    """Bars change between runs; a cache held across them would serve a stale vol."""
    _nav(seeded_store)
    _snapshot(seeded_store, iv_percentile=0.10, hv=0.42)
    _daily_closes(seeded_store, _alternating(40))
    _chain(seeded_store, [_liquid()])

    first = (screen.screen(as_of=AS_OF).survivors + screen.screen(as_of=AS_OF).incomplete)[0]

    # The same bars, rewritten with a visibly different dispersion — which is
    # what a backfill correcting a partial final bar actually does.
    with seeded_store.transaction() as connection:
        connection.executemany(
            "UPDATE bars SET close = ? WHERE contract_id = 1 AND freq = '1d' AND ts = ?",
            [
                (close, (SNAPSHOT - timedelta(days=40 - i)).isoformat() + "T00:00:00Z")
                for i, close in enumerate(_alternating(40, start=200.0, step=0.08))
            ],
        )
    second = (screen.screen(as_of=AS_OF).survivors + screen.screen(as_of=AS_OF).incomplete)[0]

    assert first.realised_vol_self is not None
    assert second.realised_vol_self is not None
    assert second.realised_vol_self != pytest.approx(first.realised_vol_self)
