"""The indicator families that need testing directly rather than through the grid.

Chiefly the IV-percentile precedence. BUILD-BRIEF's day-one plan is the vendor
figure until `data.iv_self_source_min_sessions` snapshots have accumulated, then
self-computed — and `both` reports each with its divergence, because the vendor
number has been proven unreliable for broad ETFs: it reported S&P 500 implied
vol at the hundredth percentile while VIX sat near a three-month low.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import copy
from dataclasses import replace
from datetime import date, timedelta

import pytest

from obliskiq.compute.indicators import (
    FAMILIES,
    INDICATOR_FUNCTIONS,
    STATUS_NA,
    STATUS_OK,
    IndicatorInputs,
    _expiry_matching_tenor,
    iv_percentile,
    positioning,
    positioning_change_indicator,
)
from obliskiq.config import Config

AS_OF = date(2026, 3, 2)

#: The leaves the implied term structure emits, named once here too so a test
#: cannot silently assert a subset.
_TERM_LEAVES = ("implied_vol_atm_front", "implied_vol_atm_back", "implied_vol_term_slope")
CONTRACT_ID = 1


@pytest.fixture()
def seeded(store):
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO universe (contract_id, symbol, currency, asset_class, market, "
            "sector, grid_measures) VALUES (?, 'QQQ', 'USD', 'ETF', 'us', 'technology', 1)",
            (CONTRACT_ID,),
        )
    return store


def _snapshots(store, count, *, vendor_percentile=None, start_iv=0.20):
    """`count` sessions of accumulated underlying snapshots, oldest first."""
    with store.transaction() as connection:
        connection.executemany(
            "INSERT INTO underlying_snapshots (snapshot_date, contract_id, last, "
            "implied_vol, iv_percentile_52w, source) VALUES (?, ?, 100.0, ?, ?, 'cp_api')",
            [
                (
                    (AS_OF - timedelta(days=count - n)).isoformat(),
                    CONTRACT_ID,
                    start_iv + n * 0.001,
                    vendor_percentile if n == count - 1 else None,
                )
                for n in range(count)
            ],
        )


def _inputs(store, config, source="vendor"):
    data = copy.deepcopy(config.as_dict())
    data["data"]["iv_percentile_source"] = source
    return IndicatorInputs.bind(
        store,
        Config(data, config.source),
        as_of=AS_OF,
        market="us",
        sector="technology",
        contract_id=CONTRACT_ID,
        symbol="QQQ",
        market_spec={"currency": "USD", "native_index": "SPX"},
        benchmark_contract_id=None,
        benchmark_market="us",
    )


def _by_indicator(cells):
    return {cell.indicator: cell for cell in cells}


# ── the fifteen families ──


def test_there_is_one_function_per_family():
    assert len(INDICATOR_FUNCTIONS) == len(FAMILIES) == 15


def test_every_family_is_emitted_by_some_function(seeded, config):
    emitted = set()
    for function in INDICATOR_FUNCTIONS:
        for cell in function(_inputs(seeded, config)):
            emitted.add(cell.family)
    assert emitted == set(FAMILIES)


def test_a_family_emits_the_same_leaves_whatever_it_can_measure(seeded, config):
    """A leaf missing from the grid is worse than a leaf that says why it is absent.

    A regression, and it was mine: the realised-vol family returned early when
    the close series was too short, so the range-based leaves beside it were
    never emitted at all — 100 cells with no row rather than an n/a row. The
    coverage report counts absences and cannot count what was never written, so
    the failure would have read as a smaller grid rather than as a gap.

    The invariant is per family: the set of leaf names it emits must not depend
    on how much of it could be computed. The estimators are independent, and
    gating one on another is what made them dependent.
    """
    from obliskiq.compute.indicators import realised_vol as realised_vol_family

    measured = {cell.indicator for cell in realised_vol_family(_inputs(seeded, config))}

    empty = IndicatorInputs.bind(
        seeded,
        config,
        as_of=AS_OF,
        market="us",
        sector="technology",
        contract_id=None,
        symbol=None,
        market_spec={"currency": "USD"},
        benchmark_contract_id=None,
        benchmark_market="us",
    )
    unmeasurable = realised_vol_family(empty)

    assert {cell.indicator for cell in unmeasurable} == measured
    assert all(cell.status != STATUS_OK for cell in unmeasurable)
    assert all(cell.reason and cell.reason_code for cell in unmeasurable)


def test_no_family_ever_returns_an_empty_list(seeded, config):
    """A family that emits nothing leaves a hole the coverage report cannot see."""
    for function in INDICATOR_FUNCTIONS:
        assert function(_inputs(seeded, config)), f"{function.__name__} emitted no cell"


# ── the IV percentile precedence ──


def test_the_vendor_percentile_is_used_when_served(seeded, config):
    _snapshots(seeded, 5, vendor_percentile=0.42)
    cells = _by_indicator(iv_percentile(_inputs(seeded, config, source="vendor")))

    assert cells["iv_percentile"].status == STATUS_OK
    assert cells["iv_percentile"].value == 0.42
    assert cells["iv_percentile_vendor"].value == 0.42


def test_without_a_vendor_figure_the_cell_names_the_missing_field_id(seeded, config):
    _snapshots(seeded, 5)
    cells = _by_indicator(iv_percentile(_inputs(seeded, config, source="vendor")))

    assert cells["iv_percentile"].status == STATUS_NA
    assert "no verified Client Portal field id" in cells["iv_percentile"].reason
    # The reason has to name the way out, not merely the absence: the connector
    # route can supply the figure through record_iv_percentile today.
    assert "record_iv_percentile" in cells["iv_percentile"].reason
    assert "ROADMAP.md" in cells["iv_percentile"].reason


def test_the_self_computed_source_waits_for_the_configured_session_count(seeded, config):
    """Self-computed percentiles compound from day one and are worth nothing before."""
    minimum = config.require_int("data.iv_self_source_min_sessions")
    _snapshots(seeded, minimum - 1)

    cells = _by_indicator(iv_percentile(_inputs(seeded, config, source="self")))

    assert cells["iv_percentile"].status == STATUS_NA
    assert str(minimum) in cells["iv_percentile"].reason
    assert f"{minimum - 1} of the {minimum}" in cells["iv_percentile"].reason


def test_the_source_switches_to_self_once_enough_sessions_have_accumulated(seeded, config):
    minimum = config.require_int("data.iv_self_source_min_sessions")
    _snapshots(seeded, minimum)

    cells = _by_indicator(iv_percentile(_inputs(seeded, config, source="self")))

    assert cells["iv_percentile"].status == STATUS_OK
    assert cells["iv_percentile"].value == pytest.approx(1.0), (
        "the fixture's implied vol rises monotonically, so today is the highest reading"
    )
    assert cells["iv_percentile_self"].status == STATUS_OK


def test_both_reports_each_source_and_their_divergence(seeded, config):
    """The vendor figure is not trusted on broad ETFs; the divergence is the point."""
    minimum = config.require_int("data.iv_self_source_min_sessions")
    _snapshots(seeded, minimum, vendor_percentile=0.10)

    cells = _by_indicator(iv_percentile(_inputs(seeded, config, source="both")))

    assert cells["iv_percentile_vendor"].value == 0.10
    assert cells["iv_percentile_self"].value == pytest.approx(1.0)
    assert cells["iv_percentile_divergence"].value == pytest.approx(0.10 - 1.0)


def test_both_reports_no_divergence_when_one_source_is_missing(seeded, config):
    minimum = config.require_int("data.iv_self_source_min_sessions")
    _snapshots(seeded, minimum)

    cells = _by_indicator(iv_percentile(_inputs(seeded, config, source="both")))

    assert cells["iv_percentile_divergence"].status == STATUS_NA
    assert "both sources are required" in cells["iv_percentile_divergence"].reason


def test_the_configured_source_decides_which_cell_is_the_headline(seeded, config):
    minimum = config.require_int("data.iv_self_source_min_sessions")
    _snapshots(seeded, minimum, vendor_percentile=0.10)

    on_vendor = _by_indicator(iv_percentile(_inputs(seeded, config, source="vendor")))
    on_self = _by_indicator(iv_percentile(_inputs(seeded, config, source="self")))

    assert on_vendor["iv_percentile"].value == 0.10
    assert on_self["iv_percentile"].value == pytest.approx(1.0)
    assert "iv_percentile_self" not in on_vendor
    assert "iv_percentile_vendor" not in on_self


def test_the_shipped_config_starts_on_the_vendor_source(config):
    """Day one has no accumulated snapshots, so self-computed does not yet exist."""
    assert config.require_str("data.iv_percentile_source") == "vendor"
    assert config.require_int("data.iv_self_source_min_sessions") > 0


def test_one_gap_in_the_history_does_not_block_the_switch_forever(seeded, config):
    """The switch is on ACCUMULATED snapshots, not on a perfect window.

    One session where the vendor served no implied vol would otherwise block the
    self-computed percentile permanently.
    """
    minimum = config.require_int("data.iv_self_source_min_sessions")
    _snapshots(seeded, minimum + 1)
    # Blank one session's implied vol, as a missing quote would.
    with seeded.transaction() as connection:
        connection.execute(
            "UPDATE underlying_snapshots SET implied_vol = NULL WHERE snapshot_date = ("
            "SELECT snapshot_date FROM underlying_snapshots ORDER BY snapshot_date LIMIT 1)"
        )

    cells = _by_indicator(iv_percentile(_inputs(seeded, config, source="self")))

    assert cells["iv_percentile"].status == STATUS_OK
    assert cells["iv_percentile_self"].status == STATUS_OK


def test_the_inapplicable_leaf_names_match_the_computed_ones_for_every_source(config, store):
    """Indicator 8 emits a different leaf set per data.iv_percentile_source, and the
    inapplicable path has to reproduce it exactly — otherwise a cell's leaf names
    change with its status and the rows cannot be joined across days.

    Checked for all three sources, not just the configured one: the coupling is
    what breaks when someone adds a leaf to the computing path alone.
    """
    from obliskiq.compute.indicators import _iv_percentile_leaves

    for source in ("vendor", "self", "both"):
        computed = IndicatorInputs.bind(
            store,
            config,
            as_of=date(2026, 3, 2),
            market="us",
            sector="broad",
            contract_id=1,
            symbol="SPX",
            market_spec={},
            benchmark_contract_id=None,
            benchmark_market=None,
        )
        object.__setattr__(computed, "iv_percentile_source", source)
        emitted = tuple(cell.indicator for cell in iv_percentile(computed))

        unreachable = IndicatorInputs.bind(
            store,
            config,
            as_of=date(2026, 3, 2),
            market="korea",
            sector="broad",
            contract_id=2,
            symbol="K200",
            market_spec={},
            benchmark_contract_id=None,
            benchmark_market=None,
            options_unavailable_reason="no chain is reachable",
        )
        object.__setattr__(unreachable, "iv_percentile_source", source)
        skipped = tuple(cell.indicator for cell in iv_percentile(unreachable))

        assert skipped == _iv_percentile_leaves(source), source
        assert set(skipped) == set(emitted), source


# ── indicator 9 compares like with like ──


class _Row:
    """The one field _expiry_matching_tenor reads off a chain row."""

    def __init__(self, expiry: date) -> None:
        self.expiry = expiry


def test_the_implied_leg_matches_the_realised_tenor_rather_than_the_front_month():
    """Indicator 9 subtracts a 30-day realised vol from an implied one.

    Taking the nearest listed expiry made that a three-day implied against a
    thirty-day realised on a weekly-cycle underlying — a reading of the term
    structure, not of a dislocation.
    """
    rows = [
        _Row(AS_OF + timedelta(days=3)),
        _Row(AS_OF + timedelta(days=17)),
        _Row(AS_OF + timedelta(days=31)),
        _Row(AS_OF + timedelta(days=120)),
    ]
    chosen = _expiry_matching_tenor(rows, as_of=AS_OF, tenor_days=30)
    assert chosen == AS_OF + timedelta(days=31)


def test_an_expired_contract_is_never_the_implied_leg():
    """A contract already past has no time value left to imply anything from."""
    rows = [_Row(AS_OF - timedelta(days=2)), _Row(AS_OF + timedelta(days=60))]
    assert _expiry_matching_tenor(rows, as_of=AS_OF, tenor_days=30) == AS_OF + timedelta(days=60)

    only_past = [_Row(AS_OF - timedelta(days=2))]
    assert _expiry_matching_tenor(only_past, as_of=AS_OF, tenor_days=30) is None


def test_the_closest_expiry_wins_from_either_side():
    rows = [_Row(AS_OF + timedelta(days=25)), _Row(AS_OF + timedelta(days=40))]
    assert _expiry_matching_tenor(rows, as_of=AS_OF, tenor_days=30) == AS_OF + timedelta(days=25)
    assert _expiry_matching_tenor(rows, as_of=AS_OF, tenor_days=90) == AS_OF + timedelta(days=40)


# ── indicator 15 says which way the crowd is moving, not only how big it is ──


COT_SHARE_UNITS = "net_noncommercial_share_of_open_interest"


def _cot(store, series_id, values, *, as_of=AS_OF, units=COT_SHARE_UNITS):
    """Weekly CFTC rows, oldest first, referenced Tuesday and released Friday.

    ``values[-1]`` is the newest report published on or before ``as_of``, and the
    rest step back a week at a time — the cadence the real series has, because
    the change windows are counted in weeks against release dates.
    """
    rows = []
    for index, value in enumerate(values):
        reference = as_of - timedelta(weeks=len(values) - index)
        rows.append(
            (
                series_id,
                "cftc",
                reference.isoformat(),
                (reference + timedelta(days=3)).isoformat(),
                value,
                "weekly",
                units,
            )
        )
    with store.transaction() as connection:
        connection.executemany(
            "INSERT INTO macro (series_id, source, reference_date, release_date, value, "
            "frequency, units) VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )


def _positioning_inputs(store, config, spec):
    return IndicatorInputs.bind(
        store,
        config,
        as_of=AS_OF,
        market="us",
        sector="broad",
        contract_id=None,
        symbol=None,
        market_spec=spec,
        benchmark_contract_id=None,
        benchmark_market="us",
    )


WTI = {"cot_series": "COT_WTI"}


def _change_windows(config):
    return [int(weeks) for weeks in config.require_list("commodities.cot.change_windows_weeks")]


def test_positioning_reports_a_change_beside_its_level(store, config):
    """A crowded book still building and one being dumped are the same percentile.

    That distinction is the whole point of a positioning measure: crowding does
    not time a move, and an unwind already underway is a different trade from a
    crowd still forming.
    """
    floor = config.require_int("grid.min_observations.percentile")
    _cot(store, "COT_WTI", [0.10 + 0.001 * n for n in range(floor + 20)])

    cells = _by_indicator(positioning(_positioning_inputs(store, config, WTI)))

    assert cells["positioning_percentile"].status == STATUS_OK
    for weeks in _change_windows(config):
        change = cells[positioning_change_indicator(weeks)]
        assert change.status == STATUS_OK
        assert change.value is not None
        assert change.value > 0, "a share rising every week is a crowd still building"


def test_the_change_is_the_share_differenced_not_the_percentile(store, config):
    """Measured on the open-interest-normalised share migration 015 established.

    Differencing the PERCENTILE instead would rank a past reading against today's
    distribution and call the result a flow; the leaf is in share-of-open-interest
    points, and the arithmetic is a subtraction of two collected values.
    """
    floor = config.require_int("grid.min_observations.percentile")
    values = [0.10 + 0.001 * n for n in range(floor + 20)]
    _cot(store, "COT_WTI", values)

    cells = _by_indicator(positioning(_positioning_inputs(store, config, WTI)))
    weeks = _change_windows(config)[0]
    change = cells[positioning_change_indicator(weeks)]

    # The opening endpoint is the newest report published on or before the
    # window's start, so the change is exactly the difference of two stored
    # values — not a rank, and not an average.
    assert change.value == pytest.approx(values[-1] - values[-1 - weeks], abs=1e-12)
    assert change.units == "decimal", "share points, not a percentile"
    assert cells["positioning_percentile"].units == "percentile"


def test_the_change_is_release_aligned_like_every_other_read(store, config):
    """CFTC publishes Tuesday's positions on the following Friday.

    A report referenced before ``as_of`` but published after it is a report the
    market had not seen, and putting it in a change makes the change a forecast.
    """
    floor = config.require_int("grid.min_observations.percentile")
    _cot(store, "COT_WTI", [0.10 + 0.001 * n for n in range(floor + 20)])
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO macro (series_id, source, reference_date, release_date, value, "
            "frequency, units) VALUES ('COT_WTI', 'cftc', ?, ?, 0.99, 'weekly', "
            "'net_noncommercial_share_of_open_interest')",
            ((AS_OF - timedelta(days=1)).isoformat(), (AS_OF + timedelta(days=2)).isoformat()),
        )

    cells = _by_indicator(positioning(_positioning_inputs(store, config, WTI)))
    weeks = _change_windows(config)[0]

    assert cells[positioning_change_indicator(weeks)].value < 0.5, (
        "an unpublished report was used as the newest endpoint"
    )


def test_a_change_over_too_few_readings_is_na_with_the_count(store, config):
    """A delta over two readings is a difference, not a trend."""
    floor = config.require_int("commodities.cot.change_min_observations")
    _cot(store, "COT_WTI", [0.10, 0.11])

    cells = _by_indicator(positioning(_positioning_inputs(store, config, WTI)))
    for weeks in _change_windows(config):
        change = cells[positioning_change_indicator(weeks)]
        assert change.status == STATUS_NA
        assert change.value is None
        assert str(floor) in change.reason or "no reading published" in change.reason


def test_a_change_across_a_units_change_is_refused_like_the_percentile(store, config):
    """Subtracting a share from a contract count reports the contract count."""
    floor = config.require_int("grid.min_observations.percentile")
    _cot(store, "COT_WTI", [0.10 + 0.001 * n for n in range(floor + 20)])
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO macro (series_id, source, reference_date, release_date, value, "
            "frequency, units) VALUES ('COT_WTI', 'cftc', ?, ?, 185000.0, 'weekly', "
            "'contracts_net_noncommercial')",
            ((AS_OF - timedelta(days=8)).isoformat(), (AS_OF - timedelta(days=5)).isoformat()),
        )

    cells = _by_indicator(positioning(_positioning_inputs(store, config, WTI)))
    for cell in cells.values():
        assert cell.status == STATUS_NA
        assert cell.reason_code == "mixed_units_in_window"


def test_the_change_names_the_span_it_actually_measured(store, config):
    """Window honesty: the label carries the readings and both release dates.

    The older endpoint is anchored at or before the window's start, so the span
    is never shorter than the label claims — and where a publication gap makes it
    longer, the dates say so rather than the label quietly overstating.
    """
    floor = config.require_int("grid.min_observations.percentile")
    _cot(store, "COT_WTI", [0.10 + 0.001 * n for n in range(floor + 20)])

    cells = _by_indicator(positioning(_positioning_inputs(store, config, WTI)))
    weeks = _change_windows(config)[0]
    change = cells[positioning_change_indicator(weeks)]

    assert change.n_obs is not None and change.n_obs >= 1
    assert f"{change.n_obs} weekly readings" in change.window
    assert "by release" in change.window


def test_a_market_with_no_futures_contract_has_no_change_either(store, config):
    """The same leaves whatever the family could measure, so nothing goes missing.

    A leaf never written is a hole the coverage report cannot see; a leaf written
    `inapplicable` is a declared gap. The change leaves follow the level's status
    here because the reason is the same one: no COT series is declared at all.
    """
    cells = _by_indicator(positioning(_positioning_inputs(store, config, {})))

    expected = {"positioning_percentile"} | {
        positioning_change_indicator(weeks) for weeks in _change_windows(config)
    }
    assert set(cells) == expected
    for cell in cells.values():
        assert cell.status == "inapplicable"
        assert cell.reason_code == "no_futures_contract"


def test_the_positioning_family_emits_the_same_leaves_whatever_it_measured(store, config):
    """Measurable and unmeasurable must not differ in WHICH leaves appear."""
    floor = config.require_int("grid.min_observations.percentile")
    _cot(store, "COT_WTI", [0.10 + 0.001 * n for n in range(floor + 20)])

    measured = {
        cell.indicator
        for cell in positioning(_positioning_inputs(store, config, {"cot_series": "COT_WTI"}))
    }
    unmeasured = {
        cell.indicator
        for cell in positioning(_positioning_inputs(store, config, {"cot_series": "COT_NOTHING"}))
    }
    assert measured == unmeasured


# ── n_obs has to survive the trip to the consumer ─────────────────────────────


def test_the_reading_count_reaches_the_cell_a_consumer_reads(store, config):
    """Migration 007 added `n_obs` so a reader can tell a five-year percentile
    from a twenty-one-point one. It was written by the builder and dropped by
    every reader between there and the MCP response, so the honesty it exists
    for never arrived. This asserts on the READER, which is where it was lost.
    """
    from obliskiq.compute.build_grid import read_cells

    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO grid (as_of, market, sector, indicator, indicator_family, value, "
            "status, window, units, n_obs) VALUES "
            "('2026-09-11','us','broad','realised_vol_30d_percentile','realised_vol',0.5,"
            "'ok','2y window of 30d rolling vol (16 readings)','percentile', 16)"
        )

    cell = read_cells(store, as_of=date(2026, 9, 11), market="us", sector="broad")[0]

    assert cell.n_obs == 16


def test_a_measurement_with_no_meaningful_count_carries_none_not_zero(store, config):
    """Zero readings and "this measurement has no reading count" are different
    claims, and a 0 would read as the first."""
    from obliskiq.compute.build_grid import read_cells

    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO grid (as_of, market, sector, indicator, indicator_family, value, "
            "status, units) VALUES "
            "('2026-09-11','us','broad','policy_rate','policy_rate',0.0363,'ok','decimal')"
        )

    cell = read_cells(store, as_of=date(2026, 9, 11), market="us", sector="broad")[0]

    assert cell.n_obs is None


# ── "no chain" and "a chain with no implied vol" are different gaps ───────────


def test_a_collected_but_unpriced_chain_is_not_reported_as_no_chain(store, config):
    """Different gaps, different remedies: reach the instrument, versus collect a
    priced session. Reporting the first for the second sends the reader to the
    wrong one. Measured 2026-09-11, QQQ held 466 contracts in the store while the
    cell said "no option chain for QQQ"."""
    from obliskiq.compute.indicators import implied_vol

    _seed_chain_without_iv(store)
    inputs = _inputs(store, config)

    cell = _by_indicator(implied_vol(inputs))["implied_vol_atm"]

    assert cell.value is None
    reason = cell.reason or ""
    assert "HAS a collected chain" in reason
    assert "no option chain," not in reason


def _seed_chain_without_iv(store):
    """A chain that exists and carries no implied vol on any contract."""
    with store.transaction() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO universe (contract_id, symbol, currency, asset_class, "
            "market, sector, origin) VALUES (?, 'QQQ', 'USD', 'ETF', 'us', 'technology', "
            "'declared')",
            (CONTRACT_ID,),
        )
        connection.executemany(
            "INSERT OR REPLACE INTO chain_snapshots (snapshot_date, underlying_id, "
            "contract_id, expiry, strike, right, source, iv) VALUES (?,?,?,?,?,?,?,NULL)",
            [
                (str(AS_OF), CONTRACT_ID, 900000 + n, "2026-12-18", 400.0 + n, "C", "cp_api")
                for n in range(12)
            ],
        )


# ── the skew leaf: family 7 says which way, not only how much ────────────────


def _seed_skewed_chain(store, *, deltas=True, spot=100.0):
    """A priced, delta-carrying chain on one expiry inside the tenor window.

    Puts bid over calls, which is the ordinary equity shape and gives a NEGATIVE
    risk reversal — so a test asserting the sign is asserting something.
    """
    ladder = [
        (80.0, "P", 0.42, -0.10),
        (90.0, "P", 0.36, -0.30),
        (100.0, "C", 0.30, 0.50),
        (110.0, "C", 0.27, 0.22),
        (120.0, "C", 0.26, 0.08),
    ]
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO bars (contract_id, ts, freq, close) VALUES (?, ?, '1d', ?)",
            (CONTRACT_ID, f"{AS_OF.isoformat()}T00:00:00Z", spot),
        )
        connection.executemany(
            "INSERT OR REPLACE INTO chain_snapshots (snapshot_date, underlying_id, "
            "contract_id, expiry, strike, right, bid, ask, iv, delta, open_interest, "
            "volume, source) VALUES (?,?,?,?,?,?,1.0,1.02,?,?,5000,250,'cp_api')",
            [
                (
                    str(AS_OF),
                    CONTRACT_ID,
                    910000 + n,
                    "2026-04-17",
                    strike,
                    right,
                    iv,
                    delta if deltas else None,
                )
                for n, (strike, right, iv, delta) in enumerate(ladder)
            ],
        )


def test_the_grid_publishes_the_direction_of_the_smile_and_not_only_its_level(seeded, config):
    """The collectors buy four expiries of sixty strikes on both rights and the
    grid published one ATM number off all of it."""
    from obliskiq.compute.indicators import IMPLIED_VOL_LEAVES, implied_vol

    _seed_skewed_chain(seeded)
    cells = _by_indicator(implied_vol(_inputs(seeded, config)))

    assert set(cells) == set(IMPLIED_VOL_LEAVES)
    skew = cells["implied_vol_skew"]
    assert skew.status == "ok"
    assert skew.value < 0, "puts bid over calls: the lower tail is the one being paid for"
    assert skew.family == cells["implied_vol_atm"].family, "one family, two leaves"


def test_the_skew_leaf_carries_its_units_window_and_sample(seeded, config):
    """Every leaf does, and a cell is fed by four stores on four cadences."""
    from obliskiq.compute.indicators import implied_vol

    _seed_skewed_chain(seeded)
    skew = _by_indicator(implied_vol(_inputs(seeded, config)))["implied_vol_skew"]

    assert skew.units == "decimal", "a difference of two implied vols, not a percentile"
    assert skew.n_obs == 5, "the strikes that survived hygiene and produced the two legs"
    window = skew.window or ""
    assert "delta risk reversal" in window
    assert "2026-04-17" in window, "which expiry it was read at"
    assert "strike(s) kept" in window, "the hygiene provenance implied_vol_atm reports"


def test_a_band_that_cannot_reach_the_target_delta_says_so_with_its_reach(seeded, config):
    """`n/a` with the reason and the deltas it could reach — never the nearest
    strike standing in for the one that was asked for."""
    from obliskiq.compute.indicators import implied_vol

    with seeded.transaction() as connection:
        connection.execute(
            "INSERT INTO bars (contract_id, ts, freq, close) VALUES (?, ?, '1d', 100.0)",
            (CONTRACT_ID, f"{AS_OF.isoformat()}T00:00:00Z"),
        )
        connection.executemany(
            "INSERT OR REPLACE INTO chain_snapshots (snapshot_date, underlying_id, "
            "contract_id, expiry, strike, right, bid, ask, iv, delta, open_interest, "
            "volume, source) VALUES (?,?,?,?,?,?,1.0,1.02,?,?,5000,250,'cp_api')",
            [
                (str(AS_OF), CONTRACT_ID, 920000 + n, "2026-04-17", strike, right, iv, delta)
                for n, (strike, right, iv, delta) in enumerate(
                    [
                        (96.0, "P", 0.33, -0.42),
                        (98.0, "P", 0.31, -0.46),
                        (100.0, "C", 0.30, 0.50),
                        (102.0, "C", 0.30, 0.45),
                    ]
                )
            ],
        )

    cells = _by_indicator(implied_vol(_inputs(seeded, config)))
    skew = cells["implied_vol_skew"]

    assert skew.value is None and skew.status == "n/a"
    reason = skew.reason or ""
    assert "does not reach" in reason
    assert "0.42" in reason and "0.46" in reason, "the put deltas it could actually reach"
    assert "0.45" in reason and "0.50" in reason, "and the call ones"
    assert cells["implied_vol_atm"].value is not None, "the level is still measurable"


def test_a_chain_with_no_greeks_refuses_the_skew_and_keeps_the_level(seeded, config):
    """A route that serves no delta is a different gap from a thin book, and the
    remedy is a different one: change the route, not the strike band."""
    from obliskiq.compute.indicators import implied_vol

    _seed_skewed_chain(seeded, deltas=False)
    cells = _by_indicator(implied_vol(_inputs(seeded, config)))

    assert cells["implied_vol_atm"].value is not None
    assert cells["implied_vol_skew"].value is None
    assert "carried no delta" in (cells["implied_vol_skew"].reason or "")


def test_every_leaf_is_emitted_even_where_the_cell_measures_nothing(seeded, config):
    """A grid whose leaf set changes with its status is a grid whose rows cannot
    be joined across days."""
    from obliskiq.compute.indicators import IMPLIED_VOL_LEAVES, implied_vol

    absent = _by_indicator(implied_vol(_inputs(seeded, config)))
    assert set(absent) == set(IMPLIED_VOL_LEAVES)
    assert all(cell.status == "n/a" for cell in absent.values())

    inputs = _inputs(seeded, config)
    inapplicable = _by_indicator(
        implied_vol(replace(inputs, options_unavailable_reason="no chain is listed anywhere"))
    )
    assert set(inapplicable) == set(IMPLIED_VOL_LEAVES)
    assert all(cell.status == "inapplicable" for cell in inapplicable.values())


def test_the_skew_leaf_inherits_family_sevens_freshness_inputs(config):
    """It lives under FAMILY_IMPLIED_VOL rather than in a family of its own, so
    it reads the chain-or-snapshot requirement group and MARKET_DATA_FAMILIES
    still derives it — both of which are properties of FAMILY_INPUTS, not of a
    list somebody keeps by hand."""
    from obliskiq.compute.indicators import (
        FAMILY_IMPLIED_VOL,
        FAMILY_INPUTS,
        INPUT_OPTION_CHAIN,
        INPUT_UNDERLYING_SNAPSHOT,
        MACRO_ONLY_FAMILIES,
        MARKET_DATA_FAMILIES,
    )

    assert FAMILY_INPUTS[FAMILY_IMPLIED_VOL] == ((INPUT_OPTION_CHAIN, INPUT_UNDERLYING_SNAPSHOT),)
    assert FAMILY_IMPLIED_VOL in MARKET_DATA_FAMILIES
    assert FAMILY_IMPLIED_VOL not in MACRO_ONLY_FAMILIES


def test_one_chain_read_serves_both_leaves(seeded, config, monkeypatch):
    """A cell is computed for every market x sector on the board, so a second
    chain query per cell is a cost paid 156 times for rows already in hand."""
    from obliskiq.compute import indicators

    _seed_skewed_chain(seeded)
    reads = []
    original = indicators.chain_rows

    def counting(*args, **kwargs):
        reads.append(kwargs.get("snapshot_date"))
        return original(*args, **kwargs)

    monkeypatch.setattr(indicators, "chain_rows", counting)
    indicators.implied_vol(_inputs(seeded, config))

    assert len(reads) == 1


# ── 7c. the implied term structure ──


def _seed_term_chain(store, *, expiries, spot=100.0):
    """A priced chain across several expiries, each at its own flat IV level.

    ``expiries`` is ``{"YYYY-MM-DD": atm_iv}``. Flat in strike on purpose: this
    family reads the ATM point of each expiry, and a skew would only make the
    asserted slope harder to reason about.
    """
    ladder = [(90.0, "P", -0.30), (100.0, "C", 0.50), (110.0, "C", 0.22)]
    rows = []
    n = 0
    for expiry, level in expiries.items():
        for strike, right, delta in ladder:
            rows.append((str(AS_OF), CONTRACT_ID, 920000 + n, expiry, strike, right, level, delta))
            n += 1
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO bars (contract_id, ts, freq, close) VALUES (?, ?, '1d', ?)",
            (CONTRACT_ID, f"{AS_OF.isoformat()}T00:00:00Z", spot),
        )
        connection.executemany(
            "INSERT OR REPLACE INTO chain_snapshots (snapshot_date, underlying_id, "
            "contract_id, expiry, strike, right, bid, ask, iv, delta, open_interest, "
            "volume, source) VALUES (?,?,?,?,?,?,1.0,1.02,?,?,5000,250,'cp_api')",
            rows,
        )


def test_the_term_slope_reads_two_expiries_and_names_the_dtes_it_used(seeded, config):
    """An INVERTED curve: the front prices more vol than the back.

    "Vol is high" and "vol is high AND inverted" are opposite trades, and the
    level leaf beside this one cannot tell them apart.
    """
    from obliskiq.compute.indicators import implied_vol

    _seed_term_chain(seeded, expiries={"2026-06-05": 0.40, "2026-08-29": 0.25})
    cells = _by_indicator(implied_vol(_inputs(seeded, config)))

    slope = cells["implied_vol_term_slope"]
    assert slope.status == "ok"
    assert slope.value > 0, "front over back: an inverted curve"
    assert slope.value == pytest.approx(0.40 / 0.25 - 1.0)
    assert slope.units == "ratio", "the same statistic rv_term_slope reports"
    assert cells["implied_vol_atm_front"].value == pytest.approx(0.40)
    assert cells["implied_vol_atm_back"].value == pytest.approx(0.25)
    # The window carries the two ACTUAL DTEs, which is what makes
    # get_grid_cell(history=N) able to compare one session against another.
    assert slope.window == "95d vs 180d"
    assert {cells[leaf].window for leaf in _TERM_LEAVES} == {"95d vs 180d"}


def test_an_upward_sloping_curve_gives_a_negative_slope(seeded, config):
    from obliskiq.compute.indicators import implied_vol

    _seed_term_chain(seeded, expiries={"2026-06-05": 0.20, "2026-08-29": 0.30})
    slope = _by_indicator(implied_vol(_inputs(seeded, config)))["implied_vol_term_slope"]

    assert slope.status == "ok"
    assert slope.value < 0, "back over front: the ordinary contango shape"


def test_the_window_label_is_the_dte_pair_and_the_provenance_is_not_in_it(seeded, config):
    """Provenance in the window would differ every session and nothing would compare.

    It goes in `reason`, which an `ok` cell is allowed to carry, so both facts
    travel and only one of them is the join key.
    """
    from obliskiq.compute.indicators import implied_vol

    _seed_term_chain(seeded, expiries={"2026-06-05": 0.40, "2026-08-29": 0.25})
    slope = _by_indicator(implied_vol(_inputs(seeded, config)))["implied_vol_term_slope"]

    assert slope.window == "95d vs 180d"
    assert "2026-06-05" not in slope.window
    assert "2026-06-05" in (slope.reason or "")
    assert "2026-08-29" in (slope.reason or "")
    assert "exchange-quoted" in (slope.reason or "")


def test_one_listed_expiry_is_n_a_and_never_a_slope_against_itself(seeded, config):
    from obliskiq.compute.indicators import implied_vol

    _seed_term_chain(seeded, expiries={"2026-06-05": 0.40})
    cells = _by_indicator(implied_vol(_inputs(seeded, config)))

    for leaf in _TERM_LEAVES:
        assert cells[leaf].status == "n/a"
        assert cells[leaf].value is None
    reason = cells["implied_vol_term_slope"].reason or ""
    # Named as its OWN finding rather than as a failed back leg: a name listing
    # one expiry also misses the back target, and reporting that would send a
    # reader looking for a tolerance to widen when what is missing is a second
    # expiry to widen it onto.
    assert "only one listed expiry (95d)" in reason
    assert "against itself is zero by construction" in reason


def test_two_expiries_too_close_together_are_refused_with_their_span(seeded, config):
    """A slope across 130 and 145 days is not comparable with one across 95 and 180.

    Both legs are inside their tolerance here — 130 is 35 off the front target
    and 145 is 35 off the back — so this is the separation floor firing and not
    a tolerance refusal wearing its name.
    """
    from obliskiq.compute.indicators import implied_vol

    _seed_term_chain(seeded, expiries={"2026-07-10": 0.40, "2026-07-25": 0.38})
    cells = _by_indicator(implied_vol(_inputs(seeded, config)))

    slope = cells["implied_vol_term_slope"]
    assert slope.status == "n/a"
    assert "15d apart" in (slope.reason or "")
    assert "min_separation_days" in (slope.reason or "")
    assert slope.window == "130d vs 145d", "the refusal still names what it reached"


def test_a_leg_outside_its_tolerance_names_the_dte_it_could_reach(seeded, config):
    """Taking the nearest expiry anyway would publish a different tenor pair.

    Two expiries listed, 46d and 160d. The back leg is comfortably inside its
    tolerance; the front's nearest candidate is 49 days off its target, so the
    cell refuses rather than labelling a 46-vs-160 slope as a 95-vs-180 one.
    """
    from obliskiq.compute.indicators import implied_vol

    _seed_term_chain(seeded, expiries={"2026-04-17": 0.40, "2026-08-09": 0.25})
    cells = _by_indicator(implied_vol(_inputs(seeded, config)))

    slope = cells["implied_vol_term_slope"]
    assert slope.status == "n/a"
    assert "the front leg wants an expiry within 35d of 95d" in (slope.reason or "")
    assert "nearest listed one is 46d out" in (slope.reason or "")
    assert "Collected expiries: 46d, 160d" in (slope.reason or "")
    # The ATM level beside it still measures: one leaf refusing must not take
    # the others down with it.
    assert cells["implied_vol_atm"].status == "ok"


def test_a_single_listed_expiry_is_named_as_such(seeded, config):
    """The chain collector's own band is 90-200 DTE, and on some names only one
    expiry is ever collected."""
    from obliskiq.compute.indicators import implied_vol

    _seed_skewed_chain(seeded)  # one expiry, 46 days out
    cells = _by_indicator(implied_vol(_inputs(seeded, config)))

    slope = cells["implied_vol_term_slope"]
    assert slope.status == "n/a"
    assert "only one listed expiry (46d)" in (slope.reason or "")
    assert cells["implied_vol_atm"].status == "ok"


def test_the_term_legs_have_no_vendor_or_solved_fallback(seeded, config):
    """A slope mixing a solved front with a quoted back has a methodology break
    inside one number, and implied_vol_solved is a separate column for exactly
    that reason. Here the snapshot carries both and the term leaves still refuse."""
    from obliskiq.compute.indicators import implied_vol

    with seeded.transaction() as connection:
        connection.execute(
            "INSERT INTO bars (contract_id, ts, freq, close) VALUES (?, ?, '1d', 100.0)",
            (CONTRACT_ID, f"{AS_OF.isoformat()}T00:00:00Z"),
        )
        connection.execute(
            "INSERT OR REPLACE INTO underlying_snapshots (snapshot_date, contract_id, last, "
            "implied_vol, implied_vol_source, implied_vol_solved, implied_vol_solved_source, "
            "source) VALUES (?, ?, 100.0, 0.31, 'cp_api', 0.29, 'solved_bsm_european/q_declared', "
            "'cp_api')",
            (str(AS_OF), CONTRACT_ID),
        )
    cells = _by_indicator(implied_vol(_inputs(seeded, config)))

    assert cells["implied_vol_atm"].status == "ok", "the level does have a fallback"
    assert cells["implied_vol_atm"].value == pytest.approx(0.31)
    for leaf in _TERM_LEAVES:
        assert cells[leaf].status == "n/a", leaf
    assert "no term structure to measure" in (cells["implied_vol_term_slope"].reason or "")


def test_the_term_targets_sit_inside_the_band_the_collectors_actually_buy(config):
    """The constraint that decides this whole design, pinned so it cannot drift.

    collect/chain_collector.py buys ONLY expiries inside
    tenor.entry_dte_min-entry_dte_max, so a target outside that band names
    expiries no collector has ever stored and the leaf would be n/a on every
    cell for ever with nothing saying why. Widening the targets means widening
    the collection window first.
    """
    smile = config.section("smile")
    tenor = config.section("tenor")
    floor = tenor.require_int("entry_dte_min")
    ceiling = tenor.require_int("entry_dte_max")
    front = smile.require_int("term_structure.front_target_dte")
    back = smile.require_int("term_structure.back_target_dte")

    assert floor <= front < back <= ceiling
    # And the two targets are at least the separation floor apart, or every cell
    # would refuse on a rule the config itself guaranteed it would break.
    assert back - front >= smile.require_int("term_structure.min_separation_days")


def test_the_term_leaves_inherit_family_sevens_freshness_inputs():
    """They live under FAMILY_IMPLIED_VOL rather than in a sixteenth family, so
    they read the chain-or-snapshot requirement group and MARKET_DATA_FAMILIES
    still derives them — both properties of FAMILY_INPUTS rather than of a list
    somebody keeps by hand."""
    from obliskiq.compute.indicators import (
        FAMILIES,
        FAMILY_IMPLIED_VOL,
        FAMILY_INPUTS,
        MARKET_DATA_FAMILIES,
    )

    assert len(FAMILIES) == 15, "the term structure is a leaf, not a family"
    assert FAMILY_IMPLIED_VOL in FAMILY_INPUTS
    assert FAMILY_IMPLIED_VOL in MARKET_DATA_FAMILIES


def test_the_term_legs_quote_state_sums_both_smiles(seeded, config):
    """Two status -> COUNT maps, and `_worst_status` breaks ties on the count.

    Merging them with `{**front, **back}` lets the back leg's tally overwrite
    the front's, so a front smile that is overwhelmingly FROZEN reads as live
    because the back leg happens to carry more live quotes.
    """
    from obliskiq.compute.indicators import implied_vol

    _seed_term_chain(seeded, expiries={"2026-06-05": 0.40, "2026-08-29": 0.25})
    with seeded.transaction() as connection:
        # Front: every row FROZEN. Back: mostly REALTIME with one FROZEN.
        # Summed that is 4 frozen against 2 live. MERGED, the back leg's
        # {'REALTIME': 2, 'FROZEN': 1} overwrites the front's {'FROZEN': 3}
        # wholesale and the answer flips to REALTIME on the same data.
        connection.execute(
            "UPDATE chain_snapshots SET market_data_status = 'FROZEN' WHERE expiry = '2026-06-05'"
        )
        connection.execute(
            "UPDATE chain_snapshots SET market_data_status = 'REALTIME' WHERE expiry = '2026-08-29'"
        )
        connection.execute(
            "UPDATE chain_snapshots SET market_data_status = 'FROZEN' "
            "WHERE expiry = '2026-08-29' AND strike = 90.0"
        )

    cells = _by_indicator(implied_vol(_inputs(seeded, config)))
    for leaf in _TERM_LEAVES:
        assert cells[leaf].market_data_status == "FROZEN", leaf
