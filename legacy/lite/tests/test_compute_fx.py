"""The four-way decomposition, the flags, and the carry-illusion test.

The rule that matters most: ranking consumes base real only. A market up sixty
percent in local terms can be down in euros, and the entire point of the
decomposition is that the nominal figure never reaches a decision.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from obliskiq.compute.fx_engine import (
    RANK_FIELD,
    STATUS_NA,
    STATUS_OK,
    VERDICT_ILLUSION,
    VERDICT_NA,
    VERDICT_OPPORTUNITY,
    BaseCurrencyRates,
    FxEngine,
)
from obliskiq.errors import ConfigError

AS_OF = date(2026, 3, 2)
HORIZON = 365

#: Two markets, deliberately constructed so their local nominal ordering is the
#: REVERSE of their base real ordering. BRL runs hot and depreciates; EUR does
#: neither. Any engine that ranked on the nominal figure would get this backwards.
MARKETS = {
    "eurozone": {
        "currency": "EUR",
        "native_index": "SX5E",
        "fx_series": None,
        "cpi_series": "EZ_CPI_STUB",
        "cpi_is_yoy": True,
        "policy_rate_series": "ECB_DFR",
        "long_rate_series": None,
    },
    "brazil": {
        "currency": "BRL",
        "native_index": "IBOV",
        "fx_series": "EURBRL",
        "cpi_series": "BR_CPI_STUB",
        "cpi_is_yoy": True,
        "policy_rate_series": "IRSTCI01BRM156N",
        "long_rate_series": None,
    },
}


def _instrument(store, contract_id, symbol, currency, market):
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO universe (contract_id, symbol, currency, asset_class, market, "
            "sector, is_native_index) VALUES (?, ?, ?, 'IND', ?, 'broad', 1)",
            (contract_id, symbol, currency, market),
        )


def _bars(store, contract_id, *, start_close, end_close):
    """Two endpoints a year apart. A return needs exactly that and no more."""
    with store.transaction() as connection:
        connection.executemany(
            "INSERT INTO bars (contract_id, ts, freq, close, source) VALUES (?, ?, '1d', ?, ?)",
            [
                (contract_id, (AS_OF - timedelta(days=HORIZON)).isoformat(), start_close, "cp_api"),
                (contract_id, AS_OF.isoformat(), end_close, "cp_api"),
            ],
        )


def _macro(store, series_id, source, points):
    with store.transaction() as connection:
        connection.executemany(
            "INSERT INTO macro (series_id, source, reference_date, release_date, value, "
            "frequency) VALUES (?, ?, ?, ?, ?, 'monthly')",
            [(series_id, source, day, day, value) for day, value in points],
        )


@pytest.fixture()
def fx_store(store):
    _instrument(store, 10, "SX5E", "EUR", "eurozone")
    _instrument(store, 11, "IBOV", "BRL", "brazil")

    # Euro area: up 8% nominal, 2% inflation.
    _bars(store, 10, start_close=100.0, end_close=108.0)
    # Brazil: up 60% nominal in local terms, 12% inflation, and the real has
    # weakened from 5.5 to 9.5 per euro over the year. In euros that is a LOSS,
    # which is the whole mirage in one row.
    _bars(store, 11, start_close=100.0, end_close=160.0)

    _macro(store, "EZ_CPI_STUB", "fred", [("2025-03-01", 2.0), ("2026-02-01", 2.0)])
    _macro(store, "BR_CPI_STUB", "fred", [("2025-03-01", 12.0), ("2026-02-01", 12.0)])
    _macro(
        store,
        "EURBRL",
        "ecb",
        [
            ((AS_OF - timedelta(days=3 * 365)).isoformat(), 5.0),
            ((AS_OF - timedelta(days=HORIZON)).isoformat(), 5.5),
            (AS_OF.isoformat(), 9.5),
        ],
    )
    _macro(store, "ECB_DFR", "ecb", [("2026-02-01", 2.0)])
    _macro(store, "IRSTCI01BRM156N", "fred", [("2026-02-01", 13.0)])
    return store


@pytest.fixture()
def engine(fx_store, config):
    return FxEngine(fx_store, config, MARKETS)


# ── the decomposition ──


def test_all_four_legs_are_computed(engine):
    result = engine.decompose("brazil", as_of=AS_OF, horizon_days=HORIZON)
    assert result.status == STATUS_OK
    assert result.local_nominal == pytest.approx(0.60)
    assert result.local_real is not None
    assert result.base_nominal is not None
    assert result.base_real is not None


def test_a_local_gain_becomes_a_base_loss_when_the_currency_collapses(engine):
    """The mirage, in one assertion: up 60% locally and down in euros."""
    result = engine.decompose("brazil", as_of=AS_OF, horizon_days=HORIZON)
    # 1.60 x (5.5 / 9.5) - 1 is negative: a 60% local gain is a loss in euros.
    assert result.local_nominal > 0.5
    assert result.base_nominal == pytest.approx(1.60 * (5.5 / 9.5) - 1.0)
    assert result.base_nominal < 0
    assert result.base_real < result.base_nominal


def test_the_fx_direction_is_the_reciprocal_of_the_quote(engine):
    """ECB rates are local per euro: a rising series is the local currency weakening."""
    result = engine.decompose("brazil", as_of=AS_OF, horizon_days=HORIZON)
    assert result.fx_return == pytest.approx(5.5 / 9.5 - 1.0)
    assert result.fx_return < 0, "the real weakened from 5.5 to 9.5 per euro"


def test_the_base_currency_needs_no_fx_series(engine):
    result = engine.decompose("eurozone", as_of=AS_OF, horizon_days=HORIZON)
    assert result.status == STATUS_OK
    assert result.fx_return == pytest.approx(0.0)
    assert result.base_nominal == pytest.approx(result.local_nominal)


def test_inflation_is_compounded_to_the_horizon_not_subtracted_whole(engine):
    """A year of CPI taken off a five-day return would be absurd."""
    short = engine.decompose("eurozone", as_of=AS_OF, horizon_days=5)
    year = engine.decompose("eurozone", as_of=AS_OF, horizon_days=HORIZON)
    if short.status == STATUS_OK:
        assert abs(short.local_nominal - short.local_real) < abs(
            year.local_nominal - year.local_real
        )


# ── the flags ──


def test_a_high_inflation_market_is_flagged(config, engine):
    threshold = config.require_float("fx_inflation.high_inflation_cpi_yoy")
    brazil = engine.decompose("brazil", as_of=AS_OF, horizon_days=HORIZON)
    eurozone = engine.decompose("eurozone", as_of=AS_OF, horizon_days=HORIZON)

    assert brazil.cpi_yoy >= threshold
    assert brazil.high_inflation
    assert not eurozone.high_inflation
    assert "FLAGGED" in brazil.display_note()
    assert eurozone.display_note() == ""


def test_a_depreciating_currency_is_flagged(config, engine):
    threshold = config.require_float("fx_inflation.high_depreciation_3y_annualised")
    brazil = engine.decompose("brazil", as_of=AS_OF, horizon_days=HORIZON)
    assert brazil.depreciation_annualised is not None
    assert brazil.depreciation_annualised <= -threshold
    assert brazil.high_depreciation


def test_the_flagged_market_rule_is_read_from_config(config, engine):
    assert engine.flagged_market_rule == config.require_str("fx_inflation.flagged_market_rule")


# ── ranking ──


def test_ranking_never_consumes_local_nominal(engine):
    """The headline rule. Brazil wins on nominal and loses on base real."""
    results = engine.decompose_all(as_of=AS_OF, horizon_days=HORIZON)
    by_market = {row.market: row for row in results}

    assert by_market["brazil"].local_nominal > by_market["eurozone"].local_nominal
    assert by_market["brazil"].base_real < by_market["eurozone"].base_real

    ranked = engine.rank(results)

    assert [row.market for row in ranked] == ["eurozone", "brazil"], (
        "ranking followed the nominal figure, which is exactly the mirage the "
        "four-way decomposition exists to remove"
    )
    assert RANK_FIELD == "base_real"


def test_ranking_reads_exactly_one_field(engine):
    """Constructed so that only base_real gives this order."""
    results = engine.decompose_all(as_of=AS_OF, horizon_days=HORIZON)
    ranked = engine.rank(results)
    values = [getattr(row, RANK_FIELD) for row in ranked]
    assert values == sorted(values, reverse=True)


def test_an_undecomposable_market_is_excluded_from_ranking_not_sorted_last(store, config):
    """An unmeasured market is not a poorly performing one."""
    engine = FxEngine(store, config, MARKETS)
    results = engine.decompose_all(as_of=AS_OF, horizon_days=HORIZON)
    assert all(row.status == STATUS_NA for row in results)
    assert engine.rank(results) == []
    assert all("native index" in row.reason for row in results)


def test_a_config_that_ranked_on_nominal_is_refused_at_construction(config, store):
    import copy

    from obliskiq.config import Config

    data = copy.deepcopy(config.as_dict())
    data["fx_inflation"]["rank_on"] = "local_nominal"
    with pytest.raises(ConfigError, match="mirage"):
        FxEngine(store, Config(data, config.source), MARKETS)


# ── the carry-illusion test ──


def test_a_high_nominal_yield_that_hedges_back_to_the_base_rate_is_rejected(engine):
    """Turkish bonds yield 35%, should I buy? No: that is compensation, not carry."""
    result = engine.carry_illusion("BRL", nominal_yield=0.135, as_of=AS_OF)
    assert result.verdict == VERDICT_ILLUSION
    assert result.is_illusion
    assert result.hedged_equivalent == pytest.approx(0.135 - (0.13 - 0.02))
    assert "compensation for expected depreciation" in result.reason


def test_a_genuine_excess_over_the_base_rate_survives(engine):
    result = engine.carry_illusion("BRL", nominal_yield=0.20, as_of=AS_OF)
    assert result.verdict == VERDICT_OPPORTUNITY
    assert result.hedged_equivalent > result.base_rate


def test_a_positive_excess_wiped_out_by_base_inflation_is_still_an_illusion(engine):
    """Nominal carry that is negative in real base terms is not carry."""
    result = engine.carry_illusion(
        "BRL", nominal_yield=0.135, as_of=AS_OF, local_rate=0.13, base_rate=0.00
    )
    assert result.verdict == VERDICT_ILLUSION


def test_a_missing_policy_rate_gives_na_not_the_headline(engine):
    result = engine.carry_illusion("ZAR", nominal_yield=0.35, as_of=AS_OF)
    assert result.verdict == VERDICT_NA
    assert result.hedged_equivalent is None
    assert "exactly the error the test exists to prevent" in result.reason


def test_the_engine_never_recommends_hedging(engine):
    """Currency is a measurement and ranking discipline, not an execution mandate."""
    assert engine.recommends_hedging is False


# ── persistence ──


def test_the_decomposition_round_trips_to_the_store(fx_store, engine):
    results = engine.decompose_all(as_of=AS_OF, horizon_days=HORIZON)
    assert engine.write(results, as_of=AS_OF) == len(results)

    rows = fx_store.execute(
        "SELECT market, base_real, status, reason, high_inflation FROM fx_state "
        "WHERE as_of = ? ORDER BY market",
        (AS_OF.isoformat(),),
    ).fetchall()
    assert len(rows) == len(results)
    for row in rows:
        assert row["status"] == STATUS_OK or row["reason"]


def test_writing_twice_updates_rather_than_duplicating(fx_store, engine):
    results = engine.decompose_all(as_of=AS_OF, horizon_days=HORIZON)
    engine.write(results, as_of=AS_OF)
    engine.write(results, as_of=AS_OF)
    count = fx_store.execute("SELECT COUNT(*) FROM fx_state").fetchone()[0]
    assert count == len(results)


def test_a_carry_test_is_recorded_with_its_verdict(fx_store, engine):
    engine.write_carry_test(engine.carry_illusion("BRL", nominal_yield=0.135, as_of=AS_OF))
    row = fx_store.execute("SELECT verdict, reason FROM carry_tests").fetchone()
    assert row["verdict"] == VERDICT_ILLUSION
    assert row["reason"]


# ── converting into base currency, for the affordability gates ──


def _seed_fx(store, series_id, value, *, reference="2026-08-24", release="2026-08-24"):
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO macro (series_id, source, reference_date, release_date, value, "
            "frequency, units) VALUES (?, 'ecb', ?, ?, ?, 'daily', 'rate')",
            (series_id, reference, release, value),
        )


def test_the_ecb_convention_is_inverted_exactly_once(store, config):
    """EURUSD is USD PER EURO, so a USD amount becomes EUR by DIVIDING.

    Getting this backwards is a factor of ~1.36 on EUR/USD, in the permissive
    direction, which is why the direction is decided in one place.
    """
    _seed_fx(store, "EURUSD", 1.1683)
    rates = BaseCurrencyRates(store, config)

    rate = rates.to_base("USD")
    assert rate == pytest.approx(1.0 / 1.1683)
    # The sanity check that catches an inversion: a euro is worth more than a
    # dollar here, so converting dollars into euros must SHRINK the number.
    assert 560 * rate < 560


def test_the_base_currency_converts_by_one_without_any_series(store, config):
    rates = BaseCurrencyRates(store, config)
    assert rates.to_base("EUR") == 1.0
    assert rates.to_base("eur") == 1.0


def test_an_unconfigured_currency_is_none_rather_than_parity(store, config):
    """A currency nobody declared must not silently convert at 1.0."""
    rates = BaseCurrencyRates(store, config)
    assert rates.to_base("XYZ") is None
    assert rates.to_base(None) is None


def test_a_configured_series_with_no_observation_is_none(store, config):
    """Declared but never collected is still 'unknown', not 'one'."""
    rates = BaseCurrencyRates(store, config)
    assert rates.to_base("USD") is None


def test_a_non_positive_rate_is_refused(store, config):
    """Zero is not a rate, and dividing by it is not a conversion."""
    _seed_fx(store, "EURUSD", 0.0)
    assert BaseCurrencyRates(store, config).to_base("USD") is None


def test_as_of_uses_the_rate_known_on_the_day_not_the_newest(store, config):
    """A decision priced on a past date must not convert at a rate published after it."""
    _seed_fx(store, "EURUSD", 1.10, reference="2026-01-15", release="2026-01-15")
    _seed_fx(store, "EURUSD", 1.30, reference="2026-08-20", release="2026-08-20")
    rates = BaseCurrencyRates(store, config)

    assert rates.to_base("USD", as_of=date(2026, 2, 1)) == pytest.approx(1.0 / 1.10)
    assert rates.to_base("USD") == pytest.approx(1.0 / 1.30)


def test_the_hedged_pickup_is_the_bonds_spread_over_its_own_local_rate(engine):
    """The base rate cancels, and that is CIP rather than a slip.

    hedged - base = (yield - local + base) - base = yield - local. Anyone
    "fixing" the cancellation would be adding the base rate twice, so the
    identity is pinned here. What the base rate genuinely decides is the second
    leg — the real yield after base inflation — which is not an identity.
    """
    local, base = 0.09, 0.03
    for nominal in (0.04, 0.11, 0.20):
        test = engine.carry_illusion(
            "BRL",
            nominal_yield=nominal,
            as_of=AS_OF,
            local_rate=local,
            base_rate=base,
        )
        assert test.hedged_equivalent is not None
        assert test.hedged_equivalent - base == pytest.approx(nominal - local)


def test_a_moved_base_rate_alone_does_not_flip_the_verdict(engine):
    """The consequence of the identity, stated as a behaviour.

    A yield that does not clear its own local rate is an illusion whatever the
    base rate is; the base rate only reaches the verdict through the real-yield
    leg.
    """
    verdicts = {
        engine.carry_illusion(
            "BRL", nominal_yield=0.09, as_of=AS_OF, local_rate=0.10, base_rate=base
        ).verdict
        for base in (0.0, 0.02, 0.05)
    }
    assert verdicts == {VERDICT_ILLUSION}


def test_the_result_states_what_the_hedge_cost_was_approximated_with(engine):
    """Policy rates are overnight; the yield is not. The result says so."""
    test = engine.carry_illusion(
        "BRL", nominal_yield=0.15, as_of=AS_OF, local_rate=0.10, base_rate=0.03
    )
    assert test.basis is not None
    assert "policy rate" in test.basis
    assert "HEDGE tenor" in test.basis
