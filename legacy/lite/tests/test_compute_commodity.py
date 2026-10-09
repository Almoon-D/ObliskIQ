"""The commodity layer: the curve, the wrapper, positioning, inventory, proxies.

Two rules carry most of these tests. Contango beyond the configured ceiling must
reject a futures-tracking wrapper and return an alternative, and a five-year
percentile must never be presented as cycle position — oil runs multi-decade
supercycles, and a five-year window computed today anchors on the recent regime.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from obliskiq.compute.commodity_engine import (
    REFERENCE_NONE,
    SHAPE_BACKWARDATION,
    SHAPE_CONTANGO,
    STATUS_NA,
    STATUS_OK,
    WRAPPER_FUTURES_TRACKING,
    CommodityEngine,
    CyclePosition,
)

AS_OF = date(2026, 3, 2)


def _root(store, root, *, bucket="energy", **series):
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO futures_roots (root, exchange, currency, bucket, tracked, "
            "tradable, macro_signal, untradeable_reason, cot_series, inventory_series, "
            "history_series) VALUES (?, 'NYMEX', 'USD', ?, 1, 0, 1, ?, ?, ?, ?)",
            (
                root,
                bucket,
                "futures contract prohibited under instruments.prohibited",
                series.get("cot"),
                series.get("inventory"),
                series.get("history"),
            ),
        )


def _curve(store, root, prices, *, as_of=AS_OF):
    """A ladder with the annualised roll the collector would have computed."""
    from obliskiq.collect.curve_collector import annualised_roll

    front_price = prices[0]
    rows = []
    for index, price in enumerate(prices):
        expiry = as_of + timedelta(days=30 * (index + 1))
        days_apart = (expiry - (as_of + timedelta(days=30))).days
        roll = (
            None
            if index == 0
            else annualised_roll(
                front_price=front_price,
                deferred_price=price,
                days_apart=days_apart,
                calendar_days_per_year=365.0,
            )
        )
        rows.append((as_of.isoformat(), root, expiry.isoformat(), price, roll, "cp_api"))
    with store.transaction() as connection:
        connection.executemany(
            "INSERT INTO curve_snapshots (snapshot_date, root, expiry, price, "
            "annualised_roll, source) VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )


def _macro(store, series_id, source, points, frequency="weekly"):
    with store.transaction() as connection:
        connection.executemany(
            "INSERT INTO macro (series_id, source, reference_date, release_date, value, "
            "frequency) VALUES (?, ?, ?, ?, ?, ?)",
            [(series_id, source, day, day, value, frequency) for day, value in points],
        )


@pytest.fixture()
def engine(store, config):
    return CommodityEngine(store, config)


# ── term structure and the wrapper it dictates ──


def test_a_steep_contango_rejects_the_futures_wrapper(store, config, engine):
    """The headline rule: the roll bleeds whether or not the thesis is right."""
    _root(store, "CL")
    # Deferred well above front: holding through the roll costs money.
    _curve(store, "CL", [70.0, 75.0, 80.0])

    shape = engine.curve_shape("CL", as_of=AS_OF)
    wrapper = engine.wrapper_recommendation(shape)

    assert shape.shape == SHAPE_CONTANGO
    assert shape.beyond_contango_threshold
    assert wrapper.rejects_futures_tracking
    assert wrapper.rejected == WRAPPER_FUTURES_TRACKING
    assert WRAPPER_FUTURES_TRACKING not in wrapper.alternatives
    assert wrapper.recommended is not None
    assert wrapper.recommended in config.require_list("commodities.preferred_wrapper_order")
    assert "bleeds roll" in wrapper.reason


def test_the_alternative_is_a_real_wrapper_from_the_preference_order(store, config, engine):
    _root(store, "CL")
    _curve(store, "CL", [70.0, 75.0])
    wrapper = engine.wrapper_recommendation(engine.curve_shape("CL", as_of=AS_OF))
    order = config.require_list("commodities.preferred_wrapper_order")
    assert wrapper.alternatives
    assert all(alternative in order for alternative in wrapper.alternatives)


def test_backwardation_keeps_every_wrapper_available(store, config, engine):
    """Backwardation is directionally supportive, not a dislocation warning."""
    assert config.require_bool("commodities.term_structure.backwardation_is_directional")
    _root(store, "CL")
    _curve(store, "CL", [80.0, 76.0, 73.0])

    shape = engine.curve_shape("CL", as_of=AS_OF)
    wrapper = engine.wrapper_recommendation(shape)

    assert shape.shape == SHAPE_BACKWARDATION
    assert not shape.beyond_contango_threshold
    assert wrapper.rejected is None
    assert WRAPPER_FUTURES_TRACKING in wrapper.alternatives
    assert "directionally supportive" in wrapper.reason


def test_a_shallow_contango_inside_the_ceiling_does_not_reject(store, config, engine):
    threshold = config.require_float(
        "commodities.term_structure.contango_max_annualised_for_futures_wrapper"
    )
    _root(store, "CL")
    # A fraction of a percent over a month annualises to well under the ceiling.
    _curve(store, "CL", [70.0, 70.05])
    shape = engine.curve_shape("CL", as_of=AS_OF)
    assert (shape.contango_magnitude or 0.0) < threshold
    assert not engine.wrapper_recommendation(shape).rejects_futures_tracking


def test_a_single_listed_expiry_has_no_shape_and_no_wrapper(store, engine):
    """A snapshot that cannot classify must not recommend an instrument."""
    _root(store, "CL")
    _curve(store, "CL", [70.0])

    shape = engine.curve_shape("CL", as_of=AS_OF)
    wrapper = engine.wrapper_recommendation(shape)

    assert shape.status == STATUS_NA
    assert "no shape" in shape.reason
    assert wrapper.recommended is None
    assert "Check the curve before selecting an instrument" in wrapper.reason


def test_every_state_carries_a_wrapper_recommendation(store, engine):
    """There is no path that returns a commodity view without one."""
    _root(store, "CL")
    _root(store, "GC", bucket="precious_monetary")
    _curve(store, "CL", [70.0, 75.0])

    for state in engine.state_all(as_of=AS_OF):
        assert state.wrapper is not None
        assert state.wrapper.reason


# ── search_futures stays permitted ──


def test_the_curve_is_a_data_call_and_the_contract_is_still_prohibited(store, config, engine):
    """The ban is on the instrument. Stripping the data call would blind the wrapper."""
    assert "futures_contracts" in config.require_list("instruments.prohibited")
    assert config.require_str("instruments.futures_data_use") == "allowed"

    _root(store, "CL")
    _curve(store, "CL", [70.0, 75.0])
    state = engine.state("CL", as_of=AS_OF)

    assert state.curve.annualised_roll is not None, "the curve was read"
    assert state.tradable is False, "and the contract is still not tradeable"
    assert state.macro_signal is True
    assert state.untradeable_reason


def test_search_futures_remains_reachable_in_the_collector(config):
    """The tool the curve comes from must not be stripped when implementing the ban."""
    from obliskiq.collect import source

    assert hasattr(source.MarketData, "futures_ladder")
    assert config.require_str("instruments.futures_data_use") == "allowed"


# ── positioning ──


def test_a_positioning_percentile_always_cites_its_window(store, config, engine):
    window = config.require_int("commodities.cot.percentile_window_years")
    _root(store, "CL", cot="COT_WTI")
    _macro(
        store,
        "COT_WTI",
        "cftc",
        [((AS_OF - timedelta(weeks=n)).isoformat(), float(100 - n)) for n in range(60, 0, -1)],
    )

    read = engine.positioning("CL", as_of=AS_OF)

    assert read.window_years == window
    assert str(window) in read.describe()
    assert read.observations > 0


def test_positioning_refuses_a_window_that_mixes_two_definitions(store, engine):
    """Migration 015 changed the stored quantity from a contract COUNT to a share
    of open interest. A window holding both would rank counts in the tens of
    thousands against shares in the tenths — every old row at the top, every new
    one at the bottom — and it would read as an ordinary percentile all the way
    to the output. n/a with the reason, never a number.
    """
    _root(store, "CL", cot="COT_WTI")
    with store.transaction() as connection:
        connection.executemany(
            "INSERT INTO macro (series_id, source, reference_date, release_date, value, "
            "frequency, units) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    "COT_WTI",
                    "cftc",
                    (AS_OF - timedelta(weeks=n)).isoformat(),
                    (AS_OF - timedelta(weeks=n)).isoformat(),
                    float(100 - n),
                    "weekly",
                    "contracts_net_noncommercial" if n > 30 else "share_of_open_interest",
                )
                for n in range(60, 0, -1)
            ],
        )

    read = engine.positioning("CL", as_of=AS_OF)

    assert read.percentile is None
    assert read.extreme is False
    assert "units" in (read.reason or "")
    assert "contracts_net_noncommercial" in (read.reason or "")


def test_positioning_at_an_extreme_still_requires_price_confirmation(store, config, engine):
    """Crowding makes a move violent; it does not time one."""
    assert config.require_str("commodities.cot.use") == "conditioning_not_trigger"
    _root(store, "CL", cot="COT_WTI")
    _macro(
        store,
        "COT_WTI",
        "cftc",
        [((AS_OF - timedelta(weeks=n)).isoformat(), float(60 - n)) for n in range(60, 0, -1)],
    )

    read = engine.positioning("CL", as_of=AS_OF)

    assert read.percentile == pytest.approx(1.0)
    assert read.extreme
    assert read.requires_price_confirmation
    assert "separate price confirmation is required" in read.describe()


def test_a_root_with_no_cot_series_says_so(store, engine):
    _root(store, "ZC", bucket="grains_oilseeds")
    read = engine.positioning("ZC", as_of=AS_OF)
    assert read.status == STATUS_NA
    assert read.percentile is None
    assert "no COT series is declared" in read.reason


# ── cycle position, which is NOT the five-year percentile ──


def test_cycle_position_uses_the_long_window(store, config, engine):
    monthly_years = config.require_int("commodities.history.monthly_window_years")
    cot_years = config.require_int("commodities.cot.percentile_window_years")
    assert monthly_years > cot_years

    _root(store, "CL", cot="COT_WTI", history="DCOILWTICO")
    _macro(
        store,
        "DCOILWTICO",
        "fred",
        [((AS_OF - timedelta(days=30 * n)).isoformat(), float(40 + n)) for n in range(200, 0, -1)],
        frequency="daily",
    )

    cycle = engine.cycle_position("CL", as_of=AS_OF)

    assert cycle.window_years == monthly_years
    assert cycle.window_years != cot_years
    assert str(monthly_years) in cycle.describe()


def test_a_five_year_percentile_cannot_be_constructed_as_cycle_position(config):
    """Separate types with separate windows, so one can never print as the other."""
    cot_years = config.require_int("commodities.cot.percentile_window_years")
    with pytest.raises(ValueError, match="cannot express cycle position"):
        CyclePosition(
            root="CL",
            series_id="COT_WTI",
            percentile=0.95,
            window_years=cot_years,
            observations=260,
        )


def test_positioning_and_cycle_are_different_fields_with_different_windows(store, engine):
    _root(store, "CL", cot="COT_WTI", history="DCOILWTICO")
    _curve(store, "CL", [70.0, 75.0])
    _macro(store, "COT_WTI", "cftc", [(AS_OF.isoformat(), 50.0)])
    _macro(
        store,
        "DCOILWTICO",
        "fred",
        [((AS_OF - timedelta(days=30 * n)).isoformat(), float(n)) for n in range(200, 0, -1)],
        frequency="daily",
    )

    state = engine.state("CL", as_of=AS_OF)

    assert state.positioning.window_years != state.cycle.window_years
    assert state.positioning.series_id != state.cycle.series_id


def test_a_root_with_no_long_history_refuses_to_borrow_the_short_one(store, engine):
    _root(store, "GC", bucket="precious_monetary", cot="COT_GOLD")
    _macro(store, "COT_GOLD", "cftc", [(AS_OF.isoformat(), 50.0)])
    cycle = engine.cycle_position("GC", as_of=AS_OF)
    assert cycle.percentile is None
    assert "cannot stand in for it" in cycle.reason


# ── inventory ──


def test_inventory_is_measured_against_the_seasonal_band(store, config, engine):
    """Crude builds every spring whatever the regime; a rolling window reads that
    as signal."""
    assert config.require_bool("commodities.history.seasonal_adjustment")
    window = config.require_int("commodities.inventory.seasonal_window_years")
    _root(store, "CL", inventory="WCESTUS1")

    reference_week = AS_OF.isocalendar().week
    points = []
    for year in range(AS_OF.year - window, AS_OF.year):
        # A pronounced seasonal shape: high in the same week each year.
        for week in range(1, 53):
            day = date.fromisocalendar(year, week, 1)
            seasonal_bump = 50.0 if week == reference_week else 0.0
            points.append((day.isoformat(), 400.0 + seasonal_bump + week * 0.1))
    points.append((AS_OF.isoformat(), 460.0))
    _macro(store, "WCESTUS1", "eia", points)

    read = engine.inventory("CL", as_of=AS_OF)

    assert read.reference_week == AS_OF.isocalendar().week
    assert read.window_years == window
    assert read.deviation_sd is not None
    assert "ISO week" in read.describe()


def test_the_seasonal_band_excludes_the_current_years_neighbouring_weeks(store, config, engine):
    """ "The same week in PRIOR years" — and the year filter is what makes it so.

    Matching on the ISO week alone let the current year's own weeks either side
    of the reference week into the band. Those are the two readings most
    correlated with the level being tested, so they pulled the mean towards it
    and widened the sd, understating every seasonal deviation.
    """
    halfwidth = config.require_int("commodities.inventory.seasonal_week_halfwidth")
    assert halfwidth >= 1, "with no halfwidth there are no neighbouring weeks to exclude"
    _root(store, "CL", inventory="WCESTUS1")

    reference_week = AS_OF.isocalendar().week
    reference_year = AS_OF.isocalendar().year
    points = [
        # Prior years, the same week: the band, flat at 400.
        (date.fromisocalendar(year, reference_week, 1).isoformat(), 400.0)
        for year in range(reference_year - 4, reference_year)
    ]
    # THIS year, the week before: close to today's level and not part of any
    # seasonal comparison. Including it is what the year filter prevents.
    points.append((date.fromisocalendar(reference_year, reference_week - 1, 1).isoformat(), 459.0))
    points.append((AS_OF.isoformat(), 460.0))
    _macro(store, "WCESTUS1", "eia", points)

    read = engine.inventory("CL", as_of=AS_OF)

    assert read.observations == 4, "the current year's neighbouring week entered the band"
    assert read.seasonal_mean == pytest.approx(400.0), (
        "the band mean moved towards today's level, which is the contamination"
    )


def test_a_thin_seasonal_band_reports_na_rather_than_a_deviation(store, engine):
    _root(store, "CL", inventory="WCESTUS1")
    _macro(store, "WCESTUS1", "eia", [(AS_OF.isoformat(), 460.0)])
    read = engine.inventory("CL", as_of=AS_OF)
    assert read.status == STATUS_NA
    assert read.deviation_sd is None
    assert "comparable observation" in read.reason


# ── proxies ──


def _instrument(store, contract_id, symbol, sector="materials"):
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO universe (contract_id, symbol, currency, asset_class, market, "
            "sector) VALUES (?, ?, 'USD', 'STK', 'us', ?)",
            (contract_id, symbol, sector),
        )


def _proxy_map(store, symbol, root, reference, kind, note=None):
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO commodity_proxy_map (symbol, root, reference, reference_kind, note) "
            "VALUES (?, ?, ?, ?, ?)",
            (symbol, root, reference, kind, note),
        )


def test_a_proxy_with_no_reference_series_is_unreliable_by_declaration(store, engine):
    """No free lithium price exists, and ALB is a specialty chemicals company."""
    _instrument(store, 40, "ALB")
    _proxy_map(store, "ALB", None, None, REFERENCE_NONE, "not a lithium pure-play")

    quality = engine.proxy_quality(engine.proxy_map()[0], as_of=AS_OF)

    assert not quality.reliable
    assert quality.beta is None
    assert "no free reference price series" in quality.reason
    assert "NOT a usable commodity exposure" in quality.describe()


def test_a_proxy_below_the_r_squared_floor_is_flagged(store, config, engine):
    min_r2 = config.require_float("commodities.proxies.min_r2")
    months = config.require_int("commodities.proxies.min_observations_months")
    _instrument(store, 41, "LIN")
    _proxy_map(store, "LIN", None, "DHHNGSP", "macro", "contracted pricing")

    # Equity path and commodity path deliberately unrelated.
    with store.transaction() as connection:
        connection.executemany(
            "INSERT INTO bars (contract_id, ts, freq, close, source) VALUES (?, ?, '1d', ?, ?)",
            [
                (41, (AS_OF - timedelta(days=30 * n)).isoformat(), 100.0 + (n % 3), "cp_api")
                for n in range(months + 6, 0, -1)
            ],
        )
    _macro(
        store,
        "DHHNGSP",
        "fred",
        [
            ((AS_OF - timedelta(days=30 * n)).isoformat(), 3.0 + (n % 7) * 0.5)
            for n in range(months + 6, 0, -1)
        ],
        frequency="daily",
    )

    quality = engine.proxy_quality(engine.proxy_map()[0], as_of=AS_OF)

    assert quality.r2 is not None
    assert quality.r2 < min_r2
    assert not quality.reliable
    assert "below the" in quality.reason


def test_a_clean_proxy_reports_its_beta_and_r_squared(store, config, engine):
    months = config.require_int("commodities.proxies.min_observations_months")
    _instrument(store, 42, "XLE", sector="energy")
    _proxy_map(store, "XLE", "CL", "DCOILWTICO", "macro")

    # The proxy moves with the commodity, at a beta of 2.
    levels = [50.0 * (1.02**n) for n in range(months + 6)]
    with store.transaction() as connection:
        connection.executemany(
            "INSERT INTO bars (contract_id, ts, freq, close, source) VALUES (?, ?, '1d', ?, ?)",
            [
                (
                    42,
                    (AS_OF - timedelta(days=30 * (len(levels) - n))).isoformat(),
                    level**2 / 50.0,
                    "cp_api",
                )
                for n, level in enumerate(levels)
            ],
        )
    _macro(
        store,
        "DCOILWTICO",
        "fred",
        [
            ((AS_OF - timedelta(days=30 * (len(levels) - n))).isoformat(), level)
            for n, level in enumerate(levels)
        ],
        frequency="daily",
    )

    quality = engine.proxy_quality(engine.proxy_map()[0], as_of=AS_OF)

    assert quality.observations >= months
    assert quality.beta == pytest.approx(2.0, abs=0.01)
    assert quality.reliable
    assert "beta" in quality.describe()


def test_a_proxy_with_too_few_months_is_not_regressed(store, config, engine):
    months = config.require_int("commodities.proxies.min_observations_months")
    _instrument(store, 43, "CCJ", sector="energy")
    _proxy_map(store, "CCJ", None, "DCOILWTICO", "macro")
    _macro(store, "DCOILWTICO", "fred", [(AS_OF.isoformat(), 70.0)], frequency="daily")

    quality = engine.proxy_quality(engine.proxy_map()[0], as_of=AS_OF)

    assert not quality.reliable
    assert str(months) in quality.reason
    assert "noise wearing a coefficient" in quality.reason


# ── agriculture and the untradeable set ──


def test_the_three_agri_buckets_are_declared_separately(config):
    buckets = config.require_list("commodities.buckets")
    assert "agri_inputs" in buckets
    assert "grains_oilseeds" in buckets
    assert "softs" in buckets
    assert "agriculture" not in buckets, "one agriculture bucket pools unrelated drivers"


def test_the_fertiliser_to_crop_lag_is_reported_at_every_configured_lag(store, config, engine):
    lags = config.require_list("commodities.agri_linkage.lag_months")
    _root(store, "NG", history="DHHNGSP")
    _root(store, "ZC", bucket="grains_oilseeds")
    _macro(store, "DHHNGSP", "fred", [(AS_OF.isoformat(), 3.0)], frequency="daily")

    linkage = engine.agri_linkage("ZC", as_of=AS_OF)

    assert [lag.lag_months for lag in linkage.lags] == [int(x) for x in lags]
    assert linkage.driver_series == "DHHNGSP"


def test_nitrogen_tracks_natural_gas_by_declaration(config):
    assert config.require_str("commodities.nitrogen_input_driver") == "natural_gas"


def test_every_tracked_root_is_untradeable_with_a_reason(config):
    """A futures contract is prohibited, full stop; the curve is still collected."""
    from obliskiq.collect.universe import load_universe_file

    universe = load_universe_file(config.require_str("collectors.universe.path"), config)
    for root in universe.futures_roots:
        assert not root.tradable, f"{root.root} claims to be tradeable"
        assert root.macro_signal, f"{root.root} is not even a macro signal"
        assert root.untradeable_reason, f"{root.root} gives no reason"


def test_the_softs_and_grains_name_their_downstream_equity_proxies(config):
    """Cocoa via Barry Callebaut and Lindt, coffee via JDE Peet's, grains via Bunge."""
    from obliskiq.collect.universe import load_universe_file

    universe = load_universe_file(config.require_str("collectors.universe.path"), config)
    by_root: dict[str, set[str]] = {}
    for proxy in universe.commodity_proxies:
        if proxy.root:
            by_root.setdefault(proxy.root, set()).add(proxy.symbol)

    assert {"BARN", "LISN"} <= by_root.get("CC", set())
    assert "JDEP" in by_root.get("KC", set())
    assert "BG" in by_root.get("ZC", set())


def test_olive_oil_is_not_tracked_anywhere(config):
    """No futures market, no ETF, no viable equity expression: no row."""
    from obliskiq.collect.universe import load_universe_file

    universe = load_universe_file(config.require_str("collectors.universe.path"), config)
    names = {root.root.upper() for root in universe.futures_roots}
    names |= {(root.description or "").upper() for root in universe.futures_roots}
    names |= {instrument.symbol.upper() for instrument in universe.instruments}
    assert not any("OLIVE" in name for name in names)


# ── persistence ──


def test_state_round_trips_with_its_windows_intact(store, engine):
    _root(store, "CL", cot="COT_WTI", history="DCOILWTICO")
    _curve(store, "CL", [70.0, 75.0])
    _macro(store, "COT_WTI", "cftc", [(AS_OF.isoformat(), 50.0)])

    states = engine.state_all(as_of=AS_OF)
    assert engine.write(states, as_of=AS_OF) == len(states)

    row = store.execute(
        "SELECT curve_shape, wrapper_rejected, cot_window_years, cycle_window_years, "
        "price_confirmation_required, status, reason FROM commodity_state WHERE root = 'CL'"
    ).fetchone()

    assert row["curve_shape"] == SHAPE_CONTANGO
    assert row["wrapper_rejected"] == WRAPPER_FUTURES_TRACKING
    assert row["cot_window_years"] != row["cycle_window_years"]
    assert row["price_confirmation_required"] == 1
    assert row["status"] == STATUS_OK or row["reason"]
