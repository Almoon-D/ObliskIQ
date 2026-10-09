"""Properties of the numerical core over generated inputs.

Each strategy is bounded to the region the engine is meant for, and arithmetic that can
legitimately overflow runs under ``np.errstate`` so a warning never fails a property the maths
satisfies.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import math

import numpy as np
import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from obliskiq.config import ObliskConfig
from obliskiq.quant import stats
from obliskiq.quant.bsm import (
    REASON_NO_BRACKET,
    REASON_NON_POSITIVE_PRICE,
    REASON_NOT_CONVERGED,
    REASON_OK,
    REASON_PRICE_INSENSITIVE,
    BsmInputs,
    IvSolverInputs,
    bsm_price,
    bsm_vega,
    implied_vol,
    norm_cdf,
    tau_years,
)
from obliskiq.quant.inference import benjamini_hochberg, newey_west_mean

spots = st.floats(min_value=20.0, max_value=500.0)
moneyness = st.floats(min_value=0.5, max_value=1.8)
days = st.floats(min_value=7.0, max_value=700.0)
sigmas = st.floats(min_value=0.05, max_value=2.0)
rates = st.floats(min_value=-0.01, max_value=0.08)
yields = st.floats(min_value=0.0, max_value=0.06)

#: What a model-priced contract that does not solve may legitimately report.
UNSOLVED = {
    REASON_PRICE_INSENSITIVE,
    REASON_NO_BRACKET,
    REASON_NOT_CONVERGED,
    REASON_NON_POSITIVE_PRICE,
}


@pytest.fixture(scope="module")
def params(shipped_settings: ObliskConfig) -> BsmInputs:
    return BsmInputs.from_config(shipped_settings)


@pytest.fixture(scope="module")
def solver(shipped_settings: ObliskConfig) -> IvSolverInputs:
    return IvSolverInputs.from_config(shipped_settings)


@given(spot=spots, ratio=moneyness, dte=days, sigma=sigmas, rate=rates, q=yields)
def test_put_call_parity_holds_for_every_input(params, spot, ratio, dte, sigma, rate, q):
    strike = spot * ratio
    with np.errstate(all="ignore"):
        call = float(bsm_price(spot, strike, dte, sigma, rate, q, True, params))
        put = float(bsm_price(spot, strike, dte, sigma, rate, q, False, params))
    tau = float(tau_years(dte, params))
    forward_gap = spot * math.exp(-q * tau) - strike * math.exp(-rate * tau)
    assert call - put == pytest.approx(forward_gap, abs=1e-9 * spot, rel=1e-9)


@given(
    spot=spots,
    ratio=moneyness,
    dte=days,
    low=sigmas,
    step=st.floats(min_value=0.0, max_value=1.0),
    rate=rates,
    q=yields,
    is_call=st.booleans(),
)
def test_price_never_falls_as_volatility_rises(
    params, spot, ratio, dte, low, step, rate, q, is_call
):
    strike = spot * ratio
    with np.errstate(all="ignore"):
        cheaper = float(bsm_price(spot, strike, dte, low, rate, q, is_call, params))
        dearer = float(bsm_price(spot, strike, dte, low + step, rate, q, is_call, params))
    assert dearer >= cheaper - 1e-9 * spot


@given(x=st.floats(min_value=-8.0, max_value=8.0))
def test_the_normal_cdf_is_a_probability_and_symmetric(x):
    assert 0.0 <= float(norm_cdf(x)) <= 1.0
    assert float(norm_cdf(x)) + float(norm_cdf(-x)) == pytest.approx(1.0, abs=1e-14)


@settings(max_examples=150)
@given(
    spot=spots,
    ratio=moneyness,
    dte=days,
    sigma=sigmas,
    rate=rates,
    q=yields,
    is_call=st.booleans(),
)
def test_a_solved_volatility_reprices_the_contract_and_an_unsolved_one_says_why(
    params, solver, spot, ratio, dte, sigma, rate, q, is_call
):
    strike = spot * ratio
    with np.errstate(all="ignore"):
        price = float(bsm_price(spot, strike, dte, sigma, rate, q, is_call, params))
        result = implied_vol(price, spot, strike, dte, rate, q, is_call, params, solver)
    reason = str(result.reason[0])
    if not result.converged[0]:
        assert reason in UNSOLVED
        assert math.isnan(float(result.sigma[0]))
        return
    assert reason == REASON_OK
    solved = float(result.sigma[0])
    tolerance = max(solver.price_abs_tolerance, solver.price_rel_tolerance * price) + 1e-9 * spot
    with np.errstate(all="ignore"):
        repriced = float(bsm_price(spot, strike, dte, solved, rate, q, is_call, params))
    assert abs(repriced - price) <= tolerance
    # The price is monotone in sigma, so a price error of at most `tolerance` confines the sigma
    # error to tolerance divided by the smallest slope between the true and the solved value.
    grid = np.linspace(min(sigma, solved), max(sigma, solved), 9)
    with np.errstate(all="ignore"):
        slope = (
            bsm_vega(spot, strike, dte, grid, rate, q, params) * params.vega_per_point_divisor
        ).min()
    assume(slope > 0)
    assert abs(solved - sigma) <= 1.5 * tolerance / slope + 1e-9


@given(
    spot=st.floats(min_value=50.0, max_value=200.0),
    ratio=st.floats(min_value=0.9, max_value=1.1),
    dte=st.floats(min_value=60.0, max_value=300.0),
    sigma=st.floats(min_value=0.10, max_value=0.80),
    rate=st.floats(min_value=0.0, max_value=0.05),
    q=st.floats(min_value=0.0, max_value=0.03),
    is_call=st.booleans(),
)
def test_a_well_conditioned_contract_always_round_trips(
    params, solver, spot, ratio, dte, sigma, rate, q, is_call
):
    """Near the money with a healthy vega the solver has no excuse: it recovers the sigma."""
    strike = spot * ratio
    price = float(bsm_price(spot, strike, dte, sigma, rate, q, is_call, params))
    result = implied_vol(price, spot, strike, dte, rate, q, is_call, params, solver)
    assert result.converged[0], result.reason_counts()
    assert float(result.sigma[0]) == pytest.approx(sigma, abs=5e-4)


@given(
    values=st.lists(st.floats(min_value=-5.0, max_value=5.0), min_size=6, max_size=60),
    lags=st.integers(min_value=0, max_value=12),
)
def test_the_newey_west_error_is_never_below_the_independent_floor(values, lags):
    n = len(values)
    out = newey_west_mean(values, lags, alpha=0.05, min_points=3)
    flat = newey_west_mean(values, 0, alpha=0.05, min_points=3)
    floor = math.sqrt(sum((v - sum(values) / n) ** 2 for v in values) / n / n)  # pstdev / sqrt(n)
    assert out.se == pytest.approx(flat.se) or out.se > flat.se
    assert flat.se == pytest.approx(floor, abs=1e-12)
    assert out.se >= floor - 1e-12


@given(st.lists(st.floats(min_value=0.0, max_value=1.0), min_size=1, max_size=40))
def test_benjamini_hochberg_rejects_the_smallest_p_values_first(p_values):
    flags = benjamini_hochberg(p_values, q=0.05)
    rejected = [p for p, flag in zip(p_values, flags, strict=True) if flag]
    kept = [p for p, flag in zip(p_values, flags, strict=True) if not flag]
    if rejected and kept:
        assert max(rejected) <= min(kept)


@given(
    sample=st.lists(st.floats(min_value=-100.0, max_value=100.0), min_size=1, max_size=40),
    value=st.floats(min_value=-100.0, max_value=100.0),
)
def test_a_percentile_is_a_fraction_that_rises_with_the_value(sample, value):
    here = stats.percentile_of(sample, value)
    above = stats.percentile_of(sample, value + 1.0)
    assert here is not None
    assert above is not None
    assert 0.0 <= here <= above <= 1.0


@given(st.lists(st.floats(min_value=0.5, max_value=500.0), min_size=1, max_size=40))
def test_drawdowns_are_never_positive_and_the_maximum_is_the_deeper(prices):
    today = stats.drawdown_from_peak(prices)
    worst = stats.max_drawdown(prices)
    assert today is not None
    assert worst is not None
    assert today <= 0.0
    assert worst <= today + 1e-12
