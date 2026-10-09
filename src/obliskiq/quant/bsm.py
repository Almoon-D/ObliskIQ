"""Vectorised Black-Scholes-Merton: prices, greeks and an implied-volatility solver.

Volatilities and rates are decimals (0.32 is 32 %) everywhere. Every constant is configuration
under ``numerics``.

How the solver stays honest
---------------------------

* **It reports convergence per contract.** :func:`implied_vol` returns an
  :class:`ImpliedVolResult` with a ``converged`` mask and a ``reason`` for each failure; a
  sigma that did not converge is ``nan``, never a plausible number that was merely the last
  iterate. There is no way to read a non-converged value as an implied volatility.
* **It seeds Newton from the price.** The Brenner-Subrahmanyam closed form is close to exact at
  the money, and the Manaster-Koehler moneyness term keeps it inside the bracket away from it.
  A fixed seed is available in configuration and is used for a single contract when the closed
  form cannot seed it.
* **It rejects what has no implied volatility.** A price under discounted intrinsic value, or
  above the no-arbitrage ceiling, is a data error and is refused with a named reason before any
  iteration runs.
* **It falls back to bisection.** Newton diverges where vega collapses; the contracts it does not
  solve go to bisection on ``[min_sigma, max_sigma]``, which cannot diverge.
* **It solves the out-of-the-money twin.** A deep in-the-money quote is almost entirely intrinsic
  value, so a price-scaled tolerance on it is far wider than the time value that carries the
  volatility. Every contract is converted to its out-of-the-money equivalent by put-call parity
  before solving; the implied volatility is identical by construction and the tolerance then
  applies to the quantity being solved for.
* **It reports only sigmas the price can support.** Far out of the money a near-zero price is
  consistent with a wide band of volatilities; a solved sigma whose vega is below
  ``min_vega_for_reported_iv`` is reported as price-insensitive and withheld.

The normal CDF is ``0.5 * erfc(-x / sqrt(2))``, exact to double precision. Greeks are returned
unrounded: gamma on a long-dated index option is routinely 1e-5, so rounding is a presentation
concern.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import math
from dataclasses import dataclass
from typing import NamedTuple

import numpy as np
from numpy.typing import ArrayLike, NDArray

from ..config import ObliskConfig

type FloatArray = NDArray[np.float64]
type BoolArray = NDArray[np.bool_]

_SQRT_TWO = math.sqrt(2.0)
_ERFC = np.vectorize(math.erfc, otypes=[float])

# ─────────────────────────────  config binding  ─────────────────────────────


@dataclass(frozen=True, slots=True)
class BsmInputs:
    """The pricing engine's parameters, read from ``numerics.bsm``."""

    day_count_basis: float
    min_tau_years: float
    min_sigma: float
    max_sigma: float
    min_price: float
    vega_per_point_divisor: float
    theta_per_day_divisor: float

    @classmethod
    def from_config(cls, settings: ObliskConfig) -> BsmInputs:
        bsm = settings.numerics.bsm
        return cls(
            day_count_basis=bsm.day_count_basis,
            min_tau_years=bsm.min_tau_years,
            min_sigma=bsm.min_sigma,
            max_sigma=bsm.max_sigma,
            min_price=bsm.min_price,
            vega_per_point_divisor=bsm.vega_per_point_divisor,
            theta_per_day_divisor=bsm.theta_per_day_divisor,
        )


@dataclass(frozen=True, slots=True)
class IvSolverInputs:
    """The solver's parameters, read from ``numerics.implied_vol``."""

    seed_method: str
    seed_fixed_sigma: float
    seed_min_sigma: float
    seed_max_sigma: float
    max_newton_iterations: int
    max_bisection_iterations: int
    price_abs_tolerance: float
    price_rel_tolerance: float
    min_vega: float
    min_vega_for_reported_iv: float
    reject_below_intrinsic: bool
    reject_above_upper_bound: bool

    @classmethod
    def from_config(cls, settings: ObliskConfig) -> IvSolverInputs:
        implied = settings.numerics.implied_vol
        return cls(
            seed_method=implied.seed_method,
            seed_fixed_sigma=implied.seed_fixed_sigma,
            seed_min_sigma=implied.seed_min_sigma,
            seed_max_sigma=implied.seed_max_sigma,
            max_newton_iterations=implied.max_newton_iterations,
            max_bisection_iterations=implied.max_bisection_iterations,
            price_abs_tolerance=implied.price_abs_tolerance,
            price_rel_tolerance=implied.price_rel_tolerance,
            min_vega=implied.min_vega,
            min_vega_for_reported_iv=implied.min_vega_for_reported_iv,
            reject_below_intrinsic=implied.reject_below_intrinsic,
            reject_above_upper_bound=implied.reject_above_upper_bound,
        )


# ─────────────────────────────  primitives  ─────────────────────────────


def norm_cdf(x: ArrayLike) -> FloatArray:
    """Standard normal CDF, exact to double precision."""
    return np.asarray(0.5 * _ERFC(-np.asarray(x, dtype=float) / _SQRT_TWO), dtype=float)


def norm_pdf(x: ArrayLike) -> FloatArray:
    """Standard normal PDF."""
    values = np.asarray(x, dtype=float)
    return np.asarray(np.exp(-0.5 * values * values) / math.sqrt(2.0 * math.pi), dtype=float)


def tau_years(days_to_expiry: ArrayLike, params: BsmInputs) -> FloatArray:
    """Days to expiry as a year fraction, floored at ``min_tau_years``.

    A calendar day count, as option expiries and theta are quoted; deliberately not the
    trading-day basis used to scale realised volatility, which measures diffusion over sessions
    where this measures contractual time.
    """
    days = np.asarray(days_to_expiry, dtype=float)
    return np.asarray(np.maximum(days / params.day_count_basis, params.min_tau_years), dtype=float)


class DTwo(NamedTuple):
    """d1, d2 and their normal CDFs for a set of contracts.

    ``n_d2`` is the risk-neutral probability of finishing in the money, consumed on its own, so
    it is a first-class result and not something buried inside the pricer.
    """

    d1: FloatArray
    d2: FloatArray
    n_d1: FloatArray
    n_d2: FloatArray
    n_minus_d1: FloatArray
    n_minus_d2: FloatArray


def _floored(value: ArrayLike, params: BsmInputs) -> FloatArray:
    return np.asarray(np.maximum(np.asarray(value, dtype=float), params.min_price), dtype=float)


def d1_d2(
    spot: ArrayLike,
    strike: ArrayLike,
    days_to_expiry: ArrayLike,
    sigma: ArrayLike,
    rate: ArrayLike,
    dividend_yield: ArrayLike,
    params: BsmInputs,
) -> DTwo:
    """d1, d2 and their CDFs.

    ``sigma`` is the volatility at *this* strike. Pass the strike's own smile implied
    volatility, never the at-the-money figure: constant-volatility Black-Scholes understates the
    terminal probability on out-of-the-money strikes.
    """
    spot_ = _floored(spot, params)
    strike_ = _floored(strike, params)
    tau = tau_years(days_to_expiry, params)
    vol = np.clip(np.asarray(sigma, dtype=float), params.min_sigma, params.max_sigma)
    r = np.asarray(rate, dtype=float)
    q = np.asarray(dividend_yield, dtype=float)

    vol_root_tau = vol * np.sqrt(tau)
    d1 = (np.log(spot_ / strike_) + (r - q + 0.5 * vol * vol) * tau) / vol_root_tau
    d2 = d1 - vol_root_tau
    return DTwo(
        d1=d1,
        d2=d2,
        n_d1=norm_cdf(d1),
        n_d2=norm_cdf(d2),
        n_minus_d1=norm_cdf(-d1),
        n_minus_d2=norm_cdf(-d2),
    )


def probability_itm(
    spot: ArrayLike,
    strike: ArrayLike,
    days_to_expiry: ArrayLike,
    sigma: ArrayLike,
    rate: ArrayLike,
    dividend_yield: ArrayLike,
    is_call: ArrayLike,
    params: BsmInputs,
) -> FloatArray:
    """Risk-neutral probability of expiring in the money: N(d2) for a call, N(-d2) for a put."""
    parts = d1_d2(spot, strike, days_to_expiry, sigma, rate, dividend_yield, params)
    return np.asarray(np.where(np.asarray(is_call, dtype=bool), parts.n_d2, parts.n_minus_d2))


def intrinsic_value(
    spot: ArrayLike,
    strike: ArrayLike,
    days_to_expiry: ArrayLike,
    rate: ArrayLike,
    dividend_yield: ArrayLike,
    is_call: ArrayLike,
    params: BsmInputs,
) -> FloatArray:
    """Discounted intrinsic value, the European no-arbitrage lower bound.

    For a call ``max(0, S*exp(-q*tau) - K*exp(-r*tau))``. A market price below it is not a cheap
    option, it is a bad quote.
    """
    tau = tau_years(days_to_expiry, params)
    discounted_spot = _floored(spot, params) * np.exp(
        -np.asarray(dividend_yield, dtype=float) * tau
    )
    discounted_strike = _floored(strike, params) * np.exp(-np.asarray(rate, dtype=float) * tau)
    call = np.maximum(discounted_spot - discounted_strike, 0.0)
    put = np.maximum(discounted_strike - discounted_spot, 0.0)
    return np.asarray(np.where(np.asarray(is_call, dtype=bool), call, put))


def upper_bound(
    spot: ArrayLike,
    strike: ArrayLike,
    days_to_expiry: ArrayLike,
    rate: ArrayLike,
    dividend_yield: ArrayLike,
    is_call: ArrayLike,
    params: BsmInputs,
) -> FloatArray:
    """No-arbitrage ceiling: ``S*exp(-q*tau)`` for a call, ``K*exp(-r*tau)`` for a put."""
    tau = tau_years(days_to_expiry, params)
    call = _floored(spot, params) * np.exp(-np.asarray(dividend_yield, dtype=float) * tau)
    put = _floored(strike, params) * np.exp(-np.asarray(rate, dtype=float) * tau)
    return np.asarray(np.where(np.asarray(is_call, dtype=bool), call, put))


# ─────────────────────────────  pricing and greeks  ─────────────────────────────


def bsm_price(
    spot: ArrayLike,
    strike: ArrayLike,
    days_to_expiry: ArrayLike,
    sigma: ArrayLike,
    rate: ArrayLike,
    dividend_yield: ArrayLike,
    is_call: ArrayLike,
    params: BsmInputs,
) -> FloatArray:
    """Black-Scholes-Merton price for an arbitrary basket."""
    spot_ = _floored(spot, params)
    strike_ = _floored(strike, params)
    tau = tau_years(days_to_expiry, params)
    r = np.asarray(rate, dtype=float)
    q = np.asarray(dividend_yield, dtype=float)
    parts = d1_d2(spot_, strike_, days_to_expiry, sigma, r, q, params)

    discounted_spot = spot_ * np.exp(-q * tau)
    discounted_strike = strike_ * np.exp(-r * tau)
    call = discounted_spot * parts.n_d1 - discounted_strike * parts.n_d2
    put = discounted_strike * parts.n_minus_d2 - discounted_spot * parts.n_minus_d1
    return np.asarray(np.where(np.asarray(is_call, dtype=bool), call, put))


class Greeks(NamedTuple):
    """Per-contract greeks, unrounded.

    ``vega`` and ``rho`` are per one volatility or rate point (1 % is 0.01) and ``theta`` is per
    calendar day, per ``numerics.bsm``.
    """

    delta: FloatArray
    gamma: FloatArray
    vega: FloatArray
    theta: FloatArray
    rho: FloatArray


def bsm_vega(
    spot: ArrayLike,
    strike: ArrayLike,
    days_to_expiry: ArrayLike,
    sigma: ArrayLike,
    rate: ArrayLike,
    dividend_yield: ArrayLike,
    params: BsmInputs,
) -> FloatArray:
    """Vega alone, per one volatility point; the same for a call and a put at one strike.

    Split from :func:`bsm_greeks` because the solver needs vega on every iteration and nothing
    else, and computing the other greeks only to discard them is most of the inner loop.
    """
    spot_ = _floored(spot, params)
    tau = tau_years(days_to_expiry, params)
    q = np.asarray(dividend_yield, dtype=float)
    parts = d1_d2(spot_, strike, days_to_expiry, sigma, rate, q, params)
    return np.asarray(
        spot_ * np.exp(-q * tau) * np.sqrt(tau) * norm_pdf(parts.d1) / params.vega_per_point_divisor
    )


def bsm_greeks(
    spot: ArrayLike,
    strike: ArrayLike,
    days_to_expiry: ArrayLike,
    sigma: ArrayLike,
    rate: ArrayLike,
    dividend_yield: ArrayLike,
    is_call: ArrayLike,
    params: BsmInputs,
) -> Greeks:
    """Vectorised BSM greeks; per-contract spot and rate are supported."""
    spot_ = _floored(spot, params)
    strike_ = _floored(strike, params)
    tau = tau_years(days_to_expiry, params)
    vol = np.clip(np.asarray(sigma, dtype=float), params.min_sigma, params.max_sigma)
    r = np.asarray(rate, dtype=float)
    q = np.asarray(dividend_yield, dtype=float)
    calls = np.asarray(is_call, dtype=bool)

    parts = d1_d2(spot_, strike_, days_to_expiry, vol, r, q, params)
    root_tau = np.sqrt(tau)
    pdf_d1 = norm_pdf(parts.d1)
    disc_q = np.exp(-q * tau)
    disc_r = np.exp(-r * tau)

    delta = np.where(calls, disc_q * parts.n_d1, disc_q * (parts.n_d1 - 1.0))
    gamma = (disc_q * pdf_d1) / (spot_ * vol * root_tau)
    vega = (spot_ * disc_q * root_tau * pdf_d1) / params.vega_per_point_divisor

    theta_common = -(spot_ * disc_q * pdf_d1 * vol) / (2.0 * root_tau)
    theta_call = theta_common - r * strike_ * disc_r * parts.n_d2 + q * spot_ * disc_q * parts.n_d1
    theta_put = (
        theta_common
        + r * strike_ * disc_r * parts.n_minus_d2
        - q * spot_ * disc_q * parts.n_minus_d1
    )
    theta = np.where(calls, theta_call, theta_put) / params.theta_per_day_divisor

    rho_call = strike_ * tau * disc_r * parts.n_d2 / params.vega_per_point_divisor
    rho_put = -strike_ * tau * disc_r * parts.n_minus_d2 / params.vega_per_point_divisor
    rho = np.where(calls, rho_call, rho_put)

    return Greeks(
        delta=np.asarray(delta),
        gamma=np.asarray(gamma),
        vega=np.asarray(vega),
        theta=np.asarray(theta),
        rho=np.asarray(rho),
    )


# ─────────────────────────────  implied volatility  ─────────────────────────────

#: Reasons a contract has no implied volatility. Reported per contract.
REASON_OK = "ok"
REASON_BELOW_INTRINSIC = "below_intrinsic"
REASON_ABOVE_UPPER_BOUND = "above_upper_bound"
REASON_NON_POSITIVE_PRICE = "non_positive_price"
REASON_NO_BRACKET = "no_bracket_in_sigma_bounds"
REASON_NOT_CONVERGED = "not_converged"
REASON_PRICE_INSENSITIVE = "price_insensitive_to_vol"

#: The seed methods ``numerics.implied_vol.seed_method`` may name.
SEED_BRENNER_SUBRAHMANYAM = "brenner_subrahmanyam"


@dataclass(frozen=True, slots=True)
class ImpliedVolResult:
    """Solver output.

    ``sigma`` is ``nan`` wherever ``converged`` is false: there is deliberately no way to read a
    non-converged value as if it were an implied volatility.
    """

    sigma: FloatArray
    converged: BoolArray
    reason: NDArray[np.object_]
    newton_iterations: int
    bisection_iterations: int

    @property
    def converged_count(self) -> int:
        return int(np.count_nonzero(self.converged))

    @property
    def all_converged(self) -> bool:
        return bool(np.all(self.converged))

    def reason_counts(self) -> dict[str, int]:
        values, counts = np.unique(self.reason.astype(str), return_counts=True)
        return {str(v): int(c) for v, c in zip(values, counts, strict=True)}


def brenner_subrahmanyam_seed(
    price: ArrayLike,
    spot: ArrayLike,
    strike: ArrayLike,
    days_to_expiry: ArrayLike,
    rate: ArrayLike,
    dividend_yield: ArrayLike,
    params: BsmInputs,
    solver: IvSolverInputs,
) -> FloatArray:
    """The closed-form seed ``sigma ~ sqrt(2*pi/tau) * C / F``, widened away from the money.

    Exact at the money. Away from it the bare formula reads low, so the moneyness term of the
    Manaster-Koehler seed is folded in: ``sigma_0 = sqrt(sigma_BS**2 + 2*|log(F/K)|/tau)``. The
    composite reduces to Brenner-Subrahmanyam at the money and to Manaster-Koehler deep out of
    it, which keeps the seed inside the bracket the solver needs. Where the formula is unusable
    (a non-positive forward or price) the configured fixed seed stands in for that contract only.
    """
    market = np.asarray(price, dtype=float)
    spot_ = _floored(spot, params)
    strike_ = _floored(strike, params)
    tau = tau_years(days_to_expiry, params)
    r = np.asarray(rate, dtype=float)
    q = np.asarray(dividend_yield, dtype=float)

    # Undiscount to the forward measure so calls and puts share one expression.
    forward = spot_ * np.exp((r - q) * tau)
    undiscounted = market * np.exp(r * tau)

    with np.errstate(divide="ignore", invalid="ignore"):
        seed = np.sqrt(2.0 * np.pi / tau) * (undiscounted / forward)
        moneyness = np.abs(np.log(forward / strike_))
    seed = np.sqrt(np.maximum(seed * seed + 2.0 * moneyness / tau, 0.0))

    usable = np.isfinite(seed) & (seed > 0.0) & (forward > 0.0) & (market > 0.0)
    seed = np.where(usable, seed, solver.seed_fixed_sigma)
    return np.asarray(np.clip(seed, solver.seed_min_sigma, solver.seed_max_sigma))


def to_out_of_the_money(
    price: FloatArray,
    spot: FloatArray,
    strike: FloatArray,
    days_to_expiry: FloatArray,
    rate: FloatArray,
    dividend_yield: FloatArray,
    is_call: BoolArray,
    params: BsmInputs,
) -> tuple[FloatArray, BoolArray]:
    """Convert every in-the-money contract to its out-of-the-money twin by put-call parity.

    ``C - P = S*exp(-q*tau) - K*exp(-r*tau)``, and both sides of a strike share one implied
    volatility under Black-Scholes. Solving the out-of-the-money side means the price being
    matched *is* the time value, the only part of the quote that responds to volatility.

    Returns ``(converted_price, converted_is_call)``.
    """
    tau = tau_years(days_to_expiry, params)
    forward_gap = spot * np.exp(-dividend_yield * tau) - strike * np.exp(-rate * tau)

    # A call is in the money when the discounted spot exceeds the discounted strike; a put when
    # it does not.
    itm = np.where(is_call, forward_gap > 0.0, forward_gap < 0.0)

    # call -> put subtracts the gap; put -> call adds it.
    converted = np.where(is_call, price - forward_gap, price + forward_gap)
    out_price = np.where(itm, converted, price)
    out_is_call = np.where(itm, ~is_call, is_call)

    # Parity can push a quote a hair below zero on rounding; it is zero time value.
    return np.asarray(np.maximum(out_price, 0.0)), np.asarray(out_is_call, dtype=bool)


def _broadcast(value: ArrayLike, size: int, dtype: type) -> NDArray[np.generic]:
    return np.broadcast_to(np.asarray(value, dtype=dtype), (size,)).astype(dtype)


def implied_vol(
    price: ArrayLike,
    spot: ArrayLike,
    strike: ArrayLike,
    days_to_expiry: ArrayLike,
    rate: ArrayLike,
    dividend_yield: ArrayLike,
    is_call: ArrayLike,
    params: BsmInputs,
    solver: IvSolverInputs,
) -> ImpliedVolResult:
    """Solve for implied volatility, reporting convergence per contract.

    Newton-Raphson from a closed-form seed, then bisection on ``[min_sigma, max_sigma]`` for
    whatever Newton did not solve. Prices below discounted intrinsic or above the no-arbitrage
    ceiling are rejected before either runs.

    ``price`` is the option's market price (the mid, normally) in the units of ``spot`` and
    ``strike``. Rates and the returned sigma are decimals.
    """
    market = np.atleast_1d(np.asarray(price, dtype=float))
    n = market.size
    if n == 0:
        return ImpliedVolResult(
            sigma=np.array([], dtype=float),
            converged=np.array([], dtype=bool),
            reason=np.array([], dtype=object),
            newton_iterations=0,
            bisection_iterations=0,
        )

    spot_ = np.asarray(_broadcast(spot, n, float), dtype=float)
    strike_ = np.asarray(_broadcast(strike, n, float), dtype=float)
    days = np.asarray(_broadcast(days_to_expiry, n, float), dtype=float)
    r = np.asarray(_broadcast(rate, n, float), dtype=float)
    q = np.asarray(_broadcast(dividend_yield, n, float), dtype=float)
    calls = np.asarray(_broadcast(is_call, n, bool), dtype=bool)

    sigma = np.full(n, np.nan, dtype=float)
    converged = np.zeros(n, dtype=bool)
    reason = np.full(n, REASON_NOT_CONVERGED, dtype=object)

    # ── admissibility, before any iteration ──
    live = np.isfinite(market) & (market > 0.0)
    reason[~live] = REASON_NON_POSITIVE_PRICE

    if solver.reject_below_intrinsic:
        floor = intrinsic_value(spot_, strike_, days, r, q, calls, params)
        below = live & (floor - solver.price_abs_tolerance > market)
        reason[below] = REASON_BELOW_INTRINSIC
        live &= ~below

    if solver.reject_above_upper_bound:
        ceiling = upper_bound(spot_, strike_, days, r, q, calls, params)
        above = live & (ceiling + solver.price_abs_tolerance < market)
        reason[above] = REASON_ABOVE_UPPER_BOUND
        live &= ~above

    if not np.any(live):
        return ImpliedVolResult(sigma, converged, reason, 0, 0)

    target, target_is_call = to_out_of_the_money(market, spot_, strike_, days, r, q, calls, params)
    tolerance = np.maximum(solver.price_abs_tolerance, solver.price_rel_tolerance * np.abs(target))

    def price_at(vol: FloatArray, mask: NDArray[np.intp]) -> FloatArray:
        return bsm_price(
            spot_[mask],
            strike_[mask],
            days[mask],
            vol,
            r[mask],
            q[mask],
            target_is_call[mask],
            params,
        )

    # ── Newton-Raphson ──
    if solver.seed_method == SEED_BRENNER_SUBRAHMANYAM:
        seed = brenner_subrahmanyam_seed(target, spot_, strike_, days, r, q, params, solver)
    else:
        seed = np.full(n, solver.seed_fixed_sigma, dtype=float)

    working = np.clip(seed, params.min_sigma, params.max_sigma)
    active = live.copy()
    newton_used = 0

    for step in range(solver.max_newton_iterations):
        if not np.any(active):
            break
        newton_used = step + 1
        idx = np.flatnonzero(active)
        vol = working[idx]
        diff = price_at(vol, idx) - target[idx]

        solved = np.abs(diff) <= tolerance[idx]
        if np.any(solved):
            hit = idx[solved]
            sigma[hit] = vol[solved]
            converged[hit] = True
            reason[hit] = REASON_OK
            active[hit] = False
            idx = idx[~solved]
            if idx.size == 0:
                continue
            vol = working[idx]
            diff = diff[~solved]

        # bsm_vega is per volatility point; Newton needs d(price)/d(sigma).
        slope = (
            bsm_vega(spot_[idx], strike_[idx], days[idx], vol, r[idx], q[idx], params)
            * params.vega_per_point_divisor
        )

        # Where vega has collapsed Newton is unusable: leave those to bisection.
        usable = slope > solver.min_vega
        active[idx[~usable]] = False

        movers = idx[usable]
        if movers.size:
            working[movers] = np.clip(
                working[movers] - diff[usable] / slope[usable], params.min_sigma, params.max_sigma
            )

    # ── bisection for whatever Newton did not solve ──
    remaining = live & ~converged
    bisection_used = 0
    if np.any(remaining):
        idx = np.flatnonzero(remaining)
        lo = np.full(idx.size, params.min_sigma, dtype=float)
        hi = np.full(idx.size, params.max_sigma, dtype=float)

        f_lo = price_at(lo, idx) - target[idx]
        f_hi = price_at(hi, idx) - target[idx]

        # The price is increasing in sigma, so a root exists only when the bounds straddle the
        # market price; otherwise the price is outside the configured sigma range.
        bracketed = (f_lo <= 0.0) & (f_hi >= 0.0)
        reason[idx[~bracketed]] = REASON_NO_BRACKET

        idx, lo, hi = idx[bracketed], lo[bracketed], hi[bracketed]
        for step in range(solver.max_bisection_iterations):
            if idx.size == 0:
                break
            bisection_used = step + 1
            mid = 0.5 * (lo + hi)
            f_mid = price_at(mid, idx) - target[idx]
            done = np.abs(f_mid) <= tolerance[idx]
            if np.any(done):
                hit = idx[done]
                sigma[hit] = mid[done]
                converged[hit] = True
                reason[hit] = REASON_OK
            keep = ~done
            go_up = f_mid < 0.0
            lo = np.where(go_up, mid, lo)[keep]
            hi = np.where(go_up, hi, mid)[keep]
            idx = idx[keep]

    # ── did the price carry any volatility information? ──
    # A tolerance is met trivially when vega is near zero: far out of the money a price of 1e-15
    # is consistent with a wide band of sigmas, and the solver would otherwise report whichever
    # one it landed on as if it were data.
    solved_at = np.flatnonzero(converged)
    if solved_at.size:
        vega = bsm_vega(
            spot_[solved_at],
            strike_[solved_at],
            days[solved_at],
            sigma[solved_at],
            r[solved_at],
            q[solved_at],
            params,
        )
        dead = solved_at[vega < solver.min_vega_for_reported_iv]
        sigma[dead] = np.nan
        converged[dead] = False
        reason[dead] = REASON_PRICE_INSENSITIVE

    return ImpliedVolResult(
        sigma=sigma,
        converged=converged,
        reason=reason,
        newton_iterations=newton_used,
        bisection_iterations=bisection_used,
    )
