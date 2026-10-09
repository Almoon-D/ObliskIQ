"""The Black-Scholes-Merton engine: pricing, greeks, and an implied-volatility solver that
reports what it could not solve.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from dataclasses import replace

import numpy as np
import pytest
from pydantic import ValidationError

from obliskiq.config import ObliskConfig
from obliskiq.config.schema_numerics import BsmConfig, ImpliedVolConfig, NumericsConfig
from obliskiq.quant.bsm import (
    REASON_ABOVE_UPPER_BOUND,
    REASON_BELOW_INTRINSIC,
    REASON_NO_BRACKET,
    REASON_NOT_CONVERGED,
    REASON_OK,
    REASON_PRICE_INSENSITIVE,
    BsmInputs,
    IvSolverInputs,
    brenner_subrahmanyam_seed,
    bsm_greeks,
    bsm_price,
    bsm_vega,
    d1_d2,
    implied_vol,
    intrinsic_value,
    norm_cdf,
    probability_itm,
    tau_years,
    to_out_of_the_money,
)


@pytest.fixture
def params(shipped_settings: ObliskConfig) -> BsmInputs:
    return BsmInputs.from_config(shipped_settings)


@pytest.fixture
def solver(shipped_settings: ObliskConfig) -> IvSolverInputs:
    return IvSolverInputs.from_config(shipped_settings)


ALL_CALLS = np.array([True])
ALL_PUTS = np.array([False])


# ── the normal CDF ──


def test_norm_cdf_is_exact_not_a_polynomial_approximation():
    """N(d2) is a probability a gate compares against, so a polynomial fit with an error near
    1e-7 is not good enough; this CDF is exact to double precision."""
    assert norm_cdf(0.0) == pytest.approx(0.5, abs=1e-15)
    assert norm_cdf(1.0) == pytest.approx(0.8413447460685429, abs=1e-15)
    assert norm_cdf(-1.0) == pytest.approx(0.15865525393145705, abs=1e-15)
    assert norm_cdf(-8.0) == pytest.approx(6.220960574271786e-16, rel=1e-9)


def test_norm_cdf_is_symmetric():
    x = np.linspace(-6, 6, 101)
    assert np.allclose(norm_cdf(x) + norm_cdf(-x), 1.0, atol=1e-15)


# ── pricing ──


def test_put_call_parity_holds(params):
    spot, strike, dte, sigma, r, q = 100.0, 95.0, 180.0, 0.28, 0.032, 0.015
    call = bsm_price(spot, strike, dte, sigma, r, q, True, params)
    put = bsm_price(spot, strike, dte, sigma, r, q, False, params)
    tau = float(tau_years(dte, params))
    expected = spot * np.exp(-q * tau) - strike * np.exp(-r * tau)
    assert float(call - put) == pytest.approx(expected, rel=1e-12)


def test_price_is_monotonically_increasing_in_volatility(params):
    sigmas = np.linspace(0.05, 2.0, 40)
    prices = bsm_price(100.0, 110.0, 150.0, sigmas, 0.03, 0.0, np.full(40, True), params)
    assert np.all(np.diff(prices) > 0)


def test_price_never_falls_below_discounted_intrinsic(params):
    strikes = np.linspace(40.0, 160.0, 25)
    calls = np.full(25, True)
    price = bsm_price(100.0, strikes, 200.0, 0.3, 0.03, 0.01, calls, params)
    floor = intrinsic_value(100.0, strikes, 200.0, 0.03, 0.01, calls, params)
    assert np.all(price >= floor - 1e-9)


# ── d1/d2 and N(d2) as a first-class result ──


def test_probability_itm_is_exposed_directly(params):
    """BUILD-BRIEF stores prob_target_at_entry = N(d2). It is consumed on its own,
    so it is a function, not something buried inside the pricer."""
    prob = probability_itm(100.0, 120.0, 120.0, 0.30, 0.03, 0.01, True, params)
    assert 0.0 < float(prob) < 1.0


def test_probability_itm_call_and_put_are_complementary(params):
    call = probability_itm(100.0, 105.0, 120.0, 0.3, 0.03, 0.0, True, params)
    put = probability_itm(100.0, 105.0, 120.0, 0.3, 0.03, 0.0, False, params)
    assert float(call + put) == pytest.approx(1.0, abs=1e-12)


def test_probability_itm_falls_as_the_strike_rises(params):
    strikes = np.array([90.0, 100.0, 110.0, 130.0])
    probs = probability_itm(100.0, strikes, 120.0, 0.3, 0.03, 0.0, np.full(4, True), params)
    assert np.all(np.diff(probs) < 0)


def test_smile_iv_gives_a_different_probability_than_atm_iv(params):
    """config zombie_test.prob_vol_source is strike_smile_iv, NOT atm_iv. Constant-vol
    BS understates fat-tail probability on OTM strikes and yields false zombie
    positives. This test exists so the difference is a measured fact, not a claim."""
    strike, atm_iv, smile_iv = 130.0, 0.28, 0.44
    at_atm = float(probability_itm(100.0, strike, 120.0, atm_iv, 0.03, 0.0, True, params))
    at_smile = float(probability_itm(100.0, strike, 120.0, smile_iv, 0.03, 0.0, True, params))
    assert at_smile > at_atm
    assert at_smile - at_atm > 0.02


def test_d1_d2_returns_both_and_their_cdfs(params):
    parts = d1_d2(100.0, 100.0, 120.0, 0.3, 0.03, 0.0, params)
    tau = float(tau_years(120.0, params))
    assert float(parts.d1 - parts.d2) == pytest.approx(0.3 * np.sqrt(tau), rel=1e-12)
    assert float(parts.n_d2) == pytest.approx(float(norm_cdf(parts.d2)), abs=1e-15)
    assert float(parts.n_d1 + parts.n_minus_d1) == pytest.approx(1.0, abs=1e-12)


# ── greeks ──


def test_greeks_are_not_rounded_inside_the_engine(params):
    """Gamma on a long-dated contract is routinely smaller than four decimal places, so the
    engine returns full precision and leaves rounding to whatever displays it."""
    greeks = bsm_greeks(100.0, 100.0, 700.0, 0.15, 0.03, 0.0, True, params)
    gamma = float(greeks.gamma)
    assert gamma != round(gamma, 4)
    assert gamma > 0


def test_delta_bounds_and_put_call_relation(params):
    strikes = np.linspace(50.0, 150.0, 21)
    calls = bsm_greeks(100.0, strikes, 120.0, 0.3, 0.03, 0.0, np.full(21, True), params)
    puts = bsm_greeks(100.0, strikes, 120.0, 0.3, 0.03, 0.0, np.full(21, False), params)
    assert np.all((calls.delta > 0) & (calls.delta < 1))
    assert np.all((puts.delta > -1) & (puts.delta < 0))
    tau = float(tau_years(120.0, params))
    assert np.allclose(calls.delta - puts.delta, np.exp(-0.0 * tau), atol=1e-12)


def test_gamma_and_vega_match_a_numerical_derivative(params):
    spot, strike, dte, sigma, r, q = 100.0, 105.0, 150.0, 0.3, 0.03, 0.01
    greeks = bsm_greeks(spot, strike, dte, sigma, r, q, True, params)

    h = 1e-4
    up = bsm_price(spot + h, strike, dte, sigma, r, q, True, params)
    mid = bsm_price(spot, strike, dte, sigma, r, q, True, params)
    down = bsm_price(spot - h, strike, dte, sigma, r, q, True, params)
    numerical_gamma = float((up - 2 * mid + down) / (h * h))
    assert float(greeks.gamma) == pytest.approx(numerical_gamma, rel=1e-4)

    dv = 1e-6
    vega_numerical = float(
        (
            bsm_price(spot, strike, dte, sigma + dv, r, q, True, params)
            - bsm_price(spot, strike, dte, sigma - dv, r, q, True, params)
        )
        / (2 * dv)
    )
    assert float(greeks.vega) * 100.0 == pytest.approx(vega_numerical, rel=1e-5)


def test_theta_is_negative_for_a_long_option(params):
    greeks = bsm_greeks(100.0, 100.0, 120.0, 0.3, 0.03, 0.0, True, params)
    assert float(greeks.theta) < 0


# ── the implied-volatility solver: the four fixes ──


def test_round_trip_recovers_the_input_volatility(params, solver):
    strikes = np.array([80.0, 90.0, 95.0, 100.0, 105.0, 110.0, 125.0])
    true_sigma = np.array([0.42, 0.35, 0.31, 0.29, 0.30, 0.33, 0.41])
    calls = np.full(strikes.size, True)
    prices = bsm_price(100.0, strikes, 150.0, true_sigma, 0.03, 0.01, calls, params)

    result = implied_vol(prices, 100.0, strikes, 150.0, 0.03, 0.01, calls, params, solver)
    assert result.all_converged
    assert np.allclose(result.sigma, true_sigma, atol=1e-4)


def test_round_trip_works_for_puts_too(params, solver):
    strikes = np.array([80.0, 100.0, 120.0])
    true_sigma = np.array([0.38, 0.29, 0.34])
    puts = np.full(3, False)
    prices = bsm_price(100.0, strikes, 150.0, true_sigma, 0.03, 0.01, puts, params)
    result = implied_vol(prices, 100.0, strikes, 150.0, 0.03, 0.01, puts, params, solver)
    assert result.all_converged
    assert np.allclose(result.sigma, true_sigma, atol=1e-4)


def test_non_convergence_is_reported_not_silently_returned(params, solver):
    """A clipped or diverged value must never be indistinguishable from a solved one."""
    prices = np.array([-1.0, 0.0])
    result = implied_vol(
        prices,
        100.0,
        np.array([100.0, 100.0]),
        150.0,
        0.03,
        0.0,
        np.array([True, True]),
        params,
        solver,
    )
    assert not np.any(result.converged)
    assert np.all(np.isnan(result.sigma))
    assert result.converged_count == 0


def test_the_mask_and_the_sigma_array_agree(params, solver):
    strikes = np.array([100.0, 100.0, 100.0])
    prices = np.array([8.0, -1.0, 6.0])
    result = implied_vol(prices, 100.0, strikes, 150.0, 0.03, 0.0, np.full(3, True), params, solver)
    assert np.all(np.isnan(result.sigma[~result.converged]))
    assert np.all(np.isfinite(result.sigma[result.converged]))


def test_the_seed_is_brenner_subrahmanyam_not_a_constant(params, solver, shipped_settings):
    """At the money the closed form is near-exact, so the seed must track the price
    rather than sitting at a constant."""
    assert shipped_settings.numerics.implied_vol.seed_method == "brenner_subrahmanyam"

    for true_sigma in (0.12, 0.85):
        price = bsm_price(100.0, 100.0, 90.0, true_sigma, 0.0, 0.0, True, params)
        seed = brenner_subrahmanyam_seed(
            np.atleast_1d(price), 100.0, 100.0, 90.0, 0.0, 0.0, params, solver
        )[0]
        assert seed == pytest.approx(true_sigma, rel=0.05)
        assert abs(seed - solver.seed_fixed_sigma) > 1e-6


@pytest.mark.parametrize("true_sigma", [0.06, 0.12, 0.35, 0.90, 1.80])
def test_the_seed_tracks_the_truth_across_every_volatility_regime(params, solver, true_sigma):
    """The seed's value is where it starts, not how fast Newton then runs. Newton is
    quadratically convergent, so on a benign at-the-money contract a fixed 0.35 seed
    costs little; the constant fails on the contracts at the edges of the range."""
    price = bsm_price(100.0, 100.0, 120.0, true_sigma, 0.0, 0.0, True, params)
    seed = brenner_subrahmanyam_seed(
        np.atleast_1d(price), 100.0, 100.0, 120.0, 0.0, 0.0, params, solver
    )[0]
    assert float(seed) == pytest.approx(true_sigma, rel=0.10)


def test_the_seed_always_starts_inside_the_configured_bracket(params, solver):
    """A seed outside [seed_min_sigma, seed_max_sigma] would put Newton's first step
    somewhere the solver has no reason to trust."""
    rng = np.random.default_rng(7)
    strikes = rng.uniform(50.0, 200.0, 200)
    dtes = rng.uniform(30.0, 400.0, 200)
    sigmas = rng.uniform(0.02, 3.5, 200)
    calls = rng.random(200) > 0.5
    prices = bsm_price(100.0, strikes, dtes, sigmas, 0.03, 0.0, calls, params)

    seed = brenner_subrahmanyam_seed(prices, 100.0, strikes, dtes, 0.03, 0.0, params, solver)
    assert np.all(seed >= solver.seed_min_sigma)
    assert np.all(seed <= solver.seed_max_sigma)
    assert np.all(np.isfinite(seed))


def test_a_degenerate_price_falls_back_to_the_configured_seed(params, solver):
    """Where the closed form is unusable the fixed seed is used for that contract
    only, rather than producing a nan that poisons the whole batch."""
    seed = brenner_subrahmanyam_seed(np.array([0.0]), 100.0, 100.0, 120.0, 0.0, 0.0, params, solver)
    assert float(seed[0]) == pytest.approx(solver.seed_fixed_sigma)


def test_the_seed_is_no_slower_in_aggregate(params, solver):
    """Across the volatility range the closed-form seed must not cost iterations."""
    fixed_solver = replace(solver, seed_method="fixed")
    smart_total = fixed_total = 0
    for true_sigma in (0.06, 0.09, 0.12, 0.20, 0.60, 1.00, 1.40, 2.00):
        price = bsm_price(
            100.0, np.array([100.0]), 120.0, np.array([true_sigma]), 0.03, 0.0, ALL_CALLS, params
        )
        smart = implied_vol(
            price, 100.0, np.array([100.0]), 120.0, 0.03, 0.0, ALL_CALLS, params, solver
        )
        fixed = implied_vol(
            price, 100.0, np.array([100.0]), 120.0, 0.03, 0.0, ALL_CALLS, params, fixed_solver
        )
        assert smart.converged[0]
        assert fixed.converged[0]
        smart_total += smart.newton_iterations
        fixed_total += fixed.newton_iterations
    assert smart_total <= fixed_total


def test_a_price_below_intrinsic_is_rejected(params, solver):
    """A price under discounted intrinsic has no implied volatility; it has a data error."""
    strike = np.array([50.0])
    floor = intrinsic_value(100.0, strike, 150.0, 0.03, 0.0, ALL_CALLS, params)
    result = implied_vol(floor * 0.5, 100.0, strike, 150.0, 0.03, 0.0, ALL_CALLS, params, solver)
    assert not result.converged[0]
    assert result.reason[0] == REASON_BELOW_INTRINSIC
    assert np.isnan(result.sigma[0])


def test_a_price_at_intrinsic_is_still_admissible(params, solver):
    """The rejection is for prices BELOW intrinsic. Exactly at it is a zero-vol
    boundary, not a data error."""
    strike = np.array([50.0])
    floor = intrinsic_value(100.0, strike, 150.0, 0.03, 0.0, ALL_CALLS, params)
    result = implied_vol(floor, 100.0, strike, 150.0, 0.03, 0.0, ALL_CALLS, params, solver)
    assert result.reason[0] != REASON_BELOW_INTRINSIC


def test_a_price_above_the_no_arbitrage_ceiling_is_rejected(params, solver):
    result = implied_vol(
        np.array([500.0]),
        100.0,
        np.array([100.0]),
        150.0,
        0.03,
        0.0,
        ALL_CALLS,
        params,
        solver,
    )
    assert not result.converged[0]
    assert result.reason[0] == REASON_ABOVE_UPPER_BOUND


def test_the_put_intrinsic_bound_is_used_for_puts(params, solver):
    strike = np.array([150.0])
    floor = intrinsic_value(100.0, strike, 150.0, 0.03, 0.0, ALL_PUTS, params)
    result = implied_vol(floor * 0.8, 100.0, strike, 150.0, 0.03, 0.0, ALL_PUTS, params, solver)
    assert result.reason[0] == REASON_BELOW_INTRINSIC


def test_bisection_rescues_what_newton_cannot_solve(params, solver):
    """Newton diverges where vega collapses. Bisection on a bracketed interval
    cannot, so a wide strike ladder converges end to end."""
    strikes = np.array([60.0, 80.0, 100.0, 140.0, 180.0])
    true_sigma = np.array([0.75, 0.52, 0.34, 0.48, 0.66])
    calls = np.full(5, True)
    prices = bsm_price(100.0, strikes, 200.0, true_sigma, 0.03, 0.0, calls, params)

    starved = replace(solver, max_newton_iterations=1)
    result = implied_vol(prices, 100.0, strikes, 200.0, 0.03, 0.0, calls, params, starved)
    assert result.all_converged, result.reason_counts()
    assert result.bisection_iterations > 0
    assert np.allclose(result.sigma, true_sigma, atol=1e-3)


def test_a_worthless_contract_yields_no_implied_volatility(params, solver):
    """Far out of the money a near-zero price is consistent with a wide band of
    sigmas. Reporting whichever the solver landed on would put a fabricated IV
    into the chain snapshots that the whole percentile series is built from."""
    strike = np.array([400.0])
    price = bsm_price(100.0, strike, 30.0, np.array([0.9]), 0.03, 0.0, ALL_CALLS, params)
    result = implied_vol(price, 100.0, strike, 30.0, 0.03, 0.0, ALL_CALLS, params, solver)
    assert not result.converged[0]
    assert result.reason[0] == REASON_PRICE_INSENSITIVE
    assert np.isnan(result.sigma[0])


def test_tolerance_is_relative_so_it_means_the_same_at_any_price_scale(params, solver):
    """An absolute price tolerance is meaningless across a 0.05 contract and a
    5000.00 one. Both must solve to the same accuracy in sigma."""
    for spot in (1.0, 100.0, 10000.0):
        price = bsm_price(spot, spot, 150.0, 0.33, 0.03, 0.0, True, params)
        result = implied_vol(
            np.atleast_1d(price),
            spot,
            np.array([spot]),
            150.0,
            0.03,
            0.0,
            ALL_CALLS,
            params,
            solver,
        )
        assert result.converged[0], spot
        assert float(result.sigma[0]) == pytest.approx(0.33, rel=1e-3)


def test_reasons_are_reported_per_contract(params, solver):
    strikes = np.array([100.0, 50.0, 100.0])
    good = float(bsm_price(100.0, 100.0, 150.0, 0.3, 0.03, 0.0, True, params))
    floor = float(intrinsic_value(100.0, 50.0, 150.0, 0.03, 0.0, True, params))
    prices = np.array([good, floor * 0.5, 1e9])

    result = implied_vol(prices, 100.0, strikes, 150.0, 0.03, 0.0, np.full(3, True), params, solver)
    assert list(result.reason) == [REASON_OK, REASON_BELOW_INTRINSIC, REASON_ABOVE_UPPER_BOUND]
    counts = result.reason_counts()
    assert counts[REASON_OK] == 1


def test_empty_input_is_handled(params, solver):
    result = implied_vol(
        np.array([]),
        100.0,
        np.array([]),
        np.array([]),
        0.03,
        0.0,
        np.array([], dtype=bool),
        params,
        solver,
    )
    assert result.sigma.size == 0
    assert result.all_converged


def test_solver_is_vectorised_over_a_realistic_chain(params, solver):
    """A collector snapshot is a few hundred contracts at once."""
    rng = np.random.default_rng(20260822)
    n = 400
    strikes = np.round(rng.uniform(60.0, 160.0, n), 1)
    dtes = rng.choice([95.0, 130.0, 165.0, 195.0], n)
    true_sigma = rng.uniform(0.15, 0.90, n)
    calls = rng.random(n) > 0.5
    prices = bsm_price(100.0, strikes, dtes, true_sigma, 0.03, 0.012, calls, params)

    result = implied_vol(prices, 100.0, strikes, dtes, 0.03, 0.012, calls, params, solver)
    solved = result.converged
    assert solved.sum() > n * 0.9, result.reason_counts()
    assert np.allclose(result.sigma[solved], true_sigma[solved], atol=1e-3)
    # Whatever is not solved is rejected for a stated reason, never left ambiguous.
    assert set(result.reason[~solved]) == {REASON_PRICE_INSENSITIVE}


# ── config binding ──


def test_engine_constants_come_from_config(shipped_settings, params, solver):
    numerics = shipped_settings.numerics
    assert params.day_count_basis == numerics.bsm.day_count_basis
    assert params.max_sigma == numerics.bsm.max_sigma
    assert solver.max_newton_iterations == numerics.implied_vol.max_newton_iterations
    assert solver.seed_method == numerics.implied_vol.seed_method


def test_tau_is_floored_so_an_expired_contract_does_not_divide_by_zero(params):
    assert float(tau_years(0.0, params)) == pytest.approx(params.min_tau_years)
    assert float(tau_years(-5.0, params)) == pytest.approx(params.min_tau_years)


# ── put-call parity conversion ──


def test_deep_itm_contracts_solve_through_parity(params, solver):
    """A put 57 points in the money at 95 DTE is ~99.9996% intrinsic: its entire
    time value is ~0.0002 against a price of 56.19. Solved on its own price, any
    price-scaled tolerance is orders of magnitude wider than the quantity being
    solved for. Converting to the out-of-the-money twin fixes the scale."""
    strike, dte, true_sigma = np.array([157.1]), 95.0, np.array([0.153])
    puts = np.array([False])
    price = bsm_price(100.0, strike, dte, true_sigma, 0.03, 0.012, puts, params)

    converted, converted_is_call = to_out_of_the_money(
        price,
        np.array([100.0]),
        strike,
        np.array([dte]),
        np.array([0.03]),
        np.array([0.012]),
        puts,
        params,
    )
    assert bool(converted_is_call[0]) is True, "an ITM put converts to an OTM call"
    assert converted[0] < price[0] / 1000.0, "the twin is time value only"

    # Priced as the OTM call at the same sigma, the twin must match.
    twin = bsm_price(100.0, strike, dte, true_sigma, 0.03, 0.012, np.array([True]), params)
    assert float(converted[0]) == pytest.approx(float(twin[0]), abs=1e-9)


def test_parity_conversion_leaves_otm_contracts_untouched(params):
    strikes = np.array([130.0, 70.0])
    calls = np.array([True, False])  # OTM call, OTM put
    prices = bsm_price(100.0, strikes, 150.0, 0.3, 0.03, 0.0, calls, params)
    converted, converted_is_call = to_out_of_the_money(
        prices,
        np.full(2, 100.0),
        strikes,
        np.full(2, 150.0),
        np.full(2, 0.03),
        np.full(2, 0.0),
        calls,
        params,
    )
    assert np.allclose(converted, prices)
    assert list(converted_is_call) == list(calls)


def test_an_itm_and_an_otm_quote_at_one_strike_imply_the_same_vol(params, solver):
    """Put-call parity says both sides of a strike carry one implied volatility.
    The solver must agree, or the smile would depend on which side was quoted."""
    strike, dte, true_sigma = np.array([85.0]), 150.0, np.array([0.37])
    call_price = bsm_price(100.0, strike, dte, true_sigma, 0.03, 0.01, np.array([True]), params)
    put_price = bsm_price(100.0, strike, dte, true_sigma, 0.03, 0.01, np.array([False]), params)

    from_call = implied_vol(
        call_price, 100.0, strike, dte, 0.03, 0.01, np.array([True]), params, solver
    )
    from_put = implied_vol(
        put_price, 100.0, strike, dte, 0.03, 0.01, np.array([False]), params, solver
    )
    assert from_call.converged[0]
    assert from_put.converged[0]
    assert float(from_call.sigma[0]) == pytest.approx(float(from_put.sigma[0]), abs=1e-6)
    assert float(from_call.sigma[0]) == pytest.approx(0.37, abs=1e-4)


def test_bsm_vega_matches_the_full_greeks(params):
    """bsm_vega exists so the solver does not compute five greeks to use one. It
    must agree with bsm_greeks exactly, and be right-independent."""
    strikes = np.linspace(60.0, 150.0, 19)
    calls = np.full(19, True)
    puts = np.full(19, False)

    full = bsm_greeks(100.0, strikes, 150.0, 0.32, 0.03, 0.01, calls, params)
    alone = bsm_vega(100.0, strikes, 150.0, 0.32, 0.03, 0.01, params)
    assert np.allclose(alone, full.vega, rtol=0, atol=0)

    as_puts = bsm_greeks(100.0, strikes, 150.0, 0.32, 0.03, 0.01, puts, params)
    assert np.allclose(full.vega, as_puts.vega), "vega does not depend on the right"


# ── the configuration refuses nonsense before the engine sees it ──


def _bsm(**overrides):
    fields = {
        "day_count_basis": 365.0,
        "min_tau_years": 0.0027397,
        "min_sigma": 0.005,
        "max_sigma": 4.0,
        "min_price": 0.0001,
        "vega_per_point_divisor": 100.0,
        "theta_per_day_divisor": 365.0,
    }
    fields.update(overrides)
    return fields


def test_an_inverted_sigma_range_is_refused():
    with pytest.raises(ValidationError, match="min_sigma < max_sigma"):
        BsmConfig.model_validate(_bsm(min_sigma=5.0))


def test_a_non_positive_sigma_floor_is_refused():
    with pytest.raises(ValidationError, match="min_sigma"):
        BsmConfig.model_validate(_bsm(min_sigma=0.0))


def test_a_non_positive_day_count_is_refused():
    """A zero day count would make every tau infinite."""
    with pytest.raises(ValidationError, match="day_count_basis"):
        BsmConfig.model_validate(_bsm(day_count_basis=0.0))


def test_an_unknown_seed_method_is_refused(shipped_settings):
    data = shipped_settings.numerics.implied_vol.model_dump()
    data["seed_method"] = "vibes"
    with pytest.raises(ValidationError, match="seed_method"):
        ImpliedVolConfig.model_validate(data)


def test_a_seed_range_outside_the_sigma_range_is_refused(shipped_settings):
    data = shipped_settings.numerics.model_dump()
    data["implied_vol"]["seed_max_sigma"] = data["bsm"]["max_sigma"] + 1.0
    with pytest.raises(ValidationError, match="seed range must sit inside"):
        NumericsConfig.model_validate(data)
    data = shipped_settings.numerics.model_dump()
    data["implied_vol"]["seed_min_sigma"] = data["implied_vol"]["seed_max_sigma"]
    with pytest.raises(ValidationError, match="seed_min_sigma < seed_max_sigma"):
        NumericsConfig.model_validate(data)


# ── the solver's remaining branches ──


def test_with_the_intrinsic_and_ceiling_tests_off_a_bad_price_still_yields_no_sigma(params, solver):
    """Turning the rejections off lets the iteration run; it still cannot invent a sigma."""
    lenient = replace(solver, reject_below_intrinsic=False, reject_above_upper_bound=False)
    strike = np.array([50.0])
    floor = intrinsic_value(100.0, strike, 150.0, 0.03, 0.0, ALL_CALLS, params)
    below = implied_vol(floor * 0.5, 100.0, strike, 150.0, 0.03, 0.0, ALL_CALLS, params, lenient)
    above = implied_vol(
        np.array([500.0]), 100.0, np.array([100.0]), 150.0, 0.03, 0.0, ALL_CALLS, params, lenient
    )
    for result in (below, above):
        assert not result.converged[0]
        assert np.isnan(result.sigma[0])
        assert result.reason[0] not in (REASON_BELOW_INTRINSIC, REASON_ABOVE_UPPER_BOUND)


def test_a_price_no_sigma_in_range_can_reach_has_no_bracket(params, solver):
    """An at-the-money call priced at 95 sits below the ceiling but above what the largest
    configured sigma can produce."""
    result = implied_vol(
        np.array([95.0]), 100.0, np.array([100.0]), 150.0, 0.0, 0.0, ALL_CALLS, params, solver
    )
    assert not result.converged[0]
    assert result.reason[0] == REASON_NO_BRACKET
    assert result.converged_count == 0


def test_when_newton_stalls_everywhere_bisection_solves_it(params, solver):
    """Seeded at the smallest sigma, a far strike has no vega at all: Newton cannot move and
    leaves the contract to bisection."""
    strike = np.array([135.0])
    price = bsm_price(100.0, strike, 30.0, np.array([0.45]), 0.0, 0.0, ALL_CALLS, params)
    assert float(price[0]) > solver.price_abs_tolerance * 10
    pinned = replace(solver, seed_method="fixed", seed_fixed_sigma=params.min_sigma * 4)
    result = implied_vol(price, 100.0, strike, 30.0, 0.0, 0.0, ALL_CALLS, params, pinned)
    assert result.newton_iterations == 1
    assert result.bisection_iterations > 0
    assert result.converged[0]
    assert float(result.sigma[0]) == pytest.approx(0.45, abs=1e-2)


def test_a_solver_that_runs_out_of_iterations_says_so(params, solver):
    starved = replace(solver, max_newton_iterations=1, max_bisection_iterations=1)
    strikes = np.array([60.0, 180.0])
    prices = bsm_price(
        100.0, strikes, 200.0, np.array([0.75, 0.66]), 0.03, 0.0, np.full(2, True), params
    )
    result = implied_vol(
        prices, 100.0, strikes, 200.0, 0.03, 0.0, np.full(2, True), params, starved
    )
    assert not result.all_converged
    assert set(result.reason[~result.converged]) <= {REASON_NOT_CONVERGED}
    assert np.all(np.isnan(result.sigma[~result.converged]))
    assert result.bisection_iterations == 1


def test_a_dividend_yield_and_rate_may_differ_per_contract(params, solver):
    strikes = np.array([95.0, 105.0])
    rates = np.array([0.01, 0.05])
    yields = np.array([0.0, 0.03])
    prices = bsm_price(100.0, strikes, 150.0, 0.3, rates, yields, np.full(2, True), params)
    result = implied_vol(
        prices, 100.0, strikes, 150.0, rates, yields, np.full(2, True), params, solver
    )
    assert result.all_converged
    assert np.allclose(result.sigma, 0.3, atol=1e-4)
