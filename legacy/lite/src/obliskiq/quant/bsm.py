"""Vectorised Black-Scholes-Merton, greeks, and an implied-volatility solver.

Ported from IBKR-Portmanteau server.py lines ~172-332, with the defects below
fixed. Every constant comes from ``config.yaml`` under ``numerics``.

Fixed while porting
-------------------
1. **The IV solver returned a non-converged sigma indistinguishable from a real
   one.** The original ran a fixed 12 Newton steps and returned ``sigma * 100``
   whatever happened. A clipped or diverged value looked exactly like a solved
   one. :func:`implied_vol` now returns an :class:`ImpliedVolResult` carrying a
   per-contract ``converged`` mask and a ``reason`` for each failure, and the
   non-converged entries are ``nan``, not a plausible number.
2. **The seed was a fixed 0.35.** Now Brenner-Subrahmanyam,
   ``sigma_0 ~ sqrt(2*pi/tau) * price / forward``, which is close to exact at the
   money and a far better starting point everywhere else. The fixed seed remains
   available via ``numerics.implied_vol.seed_method`` and is used automatically
   when the closed-form seed is unusable.
3. **Prices below intrinsic were solved anyway.** A price under discounted
   intrinsic, or above the no-arbitrage upper bound, has no implied volatility —
   it has a data error. Both are now rejected with a named reason.
4. **Newton alone.** Newton diverges on deep out-of-the-money strikes where vega
   collapses. Contracts Newton fails to solve now fall through to bisection on a
   bracketed interval, which cannot diverge.

Also corrected, beyond the brief
--------------------------------
* **The normal CDF was the Abramowitz-Stegun 5-term polynomial** (~7.5e-8
  absolute error), described in the original as "high-precision". N(d2) is the
  entry probability the zombie test compares against, so the approximation error
  sat directly on a discipline gate. Replaced with ``scipy.special.ndtr``, exact
  to double precision and equally vectorised.
* **Greeks were rounded to 4 decimal places inside the engine.** Gamma on a
  long-dated index option is routinely 1e-5, so rounding destroyed it. The engine
  now returns full precision; rounding is a presentation concern.
* **Deep in-the-money contracts were solved on their own price.** A put 57 points
  in the money at 95 DTE is ~99.9996% intrinsic: its entire time value can be
  0.0002 against a price of 56.19, so any price-scaled tolerance is orders of
  magnitude wider than the quantity being solved for, and the solver returns a
  confident, badly wrong sigma. Every contract is now converted to its
  out-of-the-money equivalent by put-call parity before solving. The implied
  volatility is identical by construction, and the tolerance then applies to the
  time value rather than to the intrinsic value that carries none of the
  information.
* **Time floors and sigma clamps were literals** (``0.0001``, ``0.35``, ``4.0``).
  All now read from config.

Conventions
-----------
Sigma and rates are **decimals**, not percents: 0.32 is 32% vol. The original
took percents in some functions and decimals in others, and multiplied by 100 on
the way out of the IV solver. One convention, applied everywhere.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, NamedTuple

import numpy as np
from scipy.special import ndtr  # exact normal CDF, vectorised

from ..config import Config
from ..errors import ConfigError

ArrayLike = Any


# ─────────────────────────────  config binding  ─────────────────────────────


@dataclass(frozen=True)
class BsmInputs:
    """Numerical parameters for the pricing engine, read from ``numerics.bsm``."""

    day_count_basis: float
    min_tau_years: float
    min_sigma: float
    max_sigma: float
    min_price: float
    vega_per_point_divisor: float
    theta_per_day_divisor: float

    @classmethod
    def from_config(cls, config: Config) -> BsmInputs:
        cfg = config.section("numerics.bsm")
        params = cls(
            day_count_basis=cfg.require_float("day_count_basis"),
            min_tau_years=cfg.require_float("min_tau_years"),
            min_sigma=cfg.require_float("min_sigma"),
            max_sigma=cfg.require_float("max_sigma"),
            min_price=cfg.require_float("min_price"),
            vega_per_point_divisor=cfg.require_float("vega_per_point_divisor"),
            theta_per_day_divisor=cfg.require_float("theta_per_day_divisor"),
        )
        if params.min_sigma <= 0 or params.max_sigma <= params.min_sigma:
            raise ConfigError("numerics.bsm requires 0 < min_sigma < max_sigma")
        if params.day_count_basis <= 0:
            raise ConfigError("numerics.bsm.day_count_basis must be positive")
        return params


@dataclass(frozen=True)
class IvSolverInputs:
    """Solver parameters, read from ``numerics.implied_vol``."""

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

    SEED_METHODS = ("brenner_subrahmanyam", "fixed")

    @classmethod
    def from_config(cls, config: Config) -> IvSolverInputs:
        cfg = config.section("numerics.implied_vol")
        method = cfg.require_str("seed_method")
        if method not in cls.SEED_METHODS:
            raise ConfigError(
                "numerics.implied_vol.seed_method must be one of "
                f"{cls.SEED_METHODS}, got {method!r}"
            )
        return cls(
            seed_method=method,
            seed_fixed_sigma=cfg.require_float("seed_fixed_sigma"),
            seed_min_sigma=cfg.require_float("seed_min_sigma"),
            seed_max_sigma=cfg.require_float("seed_max_sigma"),
            max_newton_iterations=cfg.require_int("max_newton_iterations"),
            max_bisection_iterations=cfg.require_int("max_bisection_iterations"),
            price_abs_tolerance=cfg.require_float("price_abs_tolerance"),
            price_rel_tolerance=cfg.require_float("price_rel_tolerance"),
            min_vega=cfg.require_float("min_vega"),
            min_vega_for_reported_iv=cfg.require_float("min_vega_for_reported_iv"),
            reject_below_intrinsic=cfg.require_bool("reject_below_intrinsic"),
            reject_above_upper_bound=cfg.require_bool("reject_above_upper_bound"),
        )


# ─────────────────────────────  primitives  ─────────────────────────────


def norm_cdf(x: ArrayLike) -> np.ndarray:
    """Standard normal CDF, exact to double precision."""
    return ndtr(np.asarray(x, dtype=float))


def norm_pdf(x: ArrayLike) -> np.ndarray:
    """Standard normal PDF."""
    arr = np.asarray(x, dtype=float)
    return np.exp(-0.5 * arr * arr) / np.sqrt(2.0 * np.pi)


def tau_years(days_to_expiry: ArrayLike, params: BsmInputs) -> np.ndarray:
    """Convert days to expiry into a year fraction, floored at ``min_tau_years``.

    Calendar day count (``day_count_basis``), matching how option expiries and
    theta are quoted. Note this is deliberately *not* the 252 trading-day basis
    used for volatility-scaled strike bounds — those measure realised diffusion
    over trading sessions, this measures contractual time to expiry.
    """
    days = np.asarray(days_to_expiry, dtype=float)
    return np.maximum(days / params.day_count_basis, params.min_tau_years)


class DTwo(NamedTuple):
    """d1, d2 and their normal CDFs for a set of contracts.

    ``n_d2`` is the risk-neutral probability of finishing in the money — the
    ``prob_target_at_entry`` the ledger stores and the zombie test compares
    against. It is exposed here as a first-class result rather than buried inside
    the pricer, because it is consumed on its own.
    """

    d1: np.ndarray
    d2: np.ndarray
    n_d1: np.ndarray
    n_d2: np.ndarray
    n_minus_d1: np.ndarray
    n_minus_d2: np.ndarray


def d1_d2(
    spot: ArrayLike,
    strike: ArrayLike,
    days_to_expiry: ArrayLike,
    sigma: ArrayLike,
    rate: ArrayLike,
    dividend_yield: ArrayLike,
    params: BsmInputs,
) -> DTwo:
    """Compute d1, d2 and their CDFs.

    ``sigma`` is the volatility to use at *this* strike. Pass the strike's own
    smile implied volatility, never the at-the-money figure: constant-vol
    Black-Scholes understates the terminal probability on out-of-the-money strikes
    and produces false zombie positives. See ``zombie_test.prob_vol_source``.

    All of ``sigma``, ``rate`` and ``dividend_yield`` are decimals.
    """
    S = np.maximum(np.asarray(spot, dtype=float), params.min_price)
    K = np.maximum(np.asarray(strike, dtype=float), params.min_price)
    tau = tau_years(days_to_expiry, params)
    vol = np.clip(np.asarray(sigma, dtype=float), params.min_sigma, params.max_sigma)
    r = np.asarray(rate, dtype=float)
    q = np.asarray(dividend_yield, dtype=float)

    vol_root_tau = vol * np.sqrt(tau)
    d1 = (np.log(S / K) + (r - q + 0.5 * vol * vol) * tau) / vol_root_tau
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
) -> np.ndarray:
    """Risk-neutral probability of expiring in the money: N(d2) call, N(-d2) put.

    This is P in the ledger's ``prob_target_at_entry`` and in the zombie test's
    ``max_prob_target`` / ``max_prob_ratio_vs_entry``. Feed it the strike's smile
    implied volatility.
    """
    parts = d1_d2(spot, strike, days_to_expiry, sigma, rate, dividend_yield, params)
    return np.where(np.asarray(is_call, dtype=bool), parts.n_d2, parts.n_minus_d2)


def intrinsic_value(
    spot: ArrayLike,
    strike: ArrayLike,
    days_to_expiry: ArrayLike,
    rate: ArrayLike,
    dividend_yield: ArrayLike,
    is_call: ArrayLike,
    params: BsmInputs,
) -> np.ndarray:
    """Discounted intrinsic value — the European no-arbitrage lower bound.

    For a call: ``max(0, S*exp(-q*tau) - K*exp(-r*tau))``. A market price below
    this is not a cheap option, it is a bad quote.
    """
    S = np.maximum(np.asarray(spot, dtype=float), params.min_price)
    K = np.maximum(np.asarray(strike, dtype=float), params.min_price)
    tau = tau_years(days_to_expiry, params)
    r = np.asarray(rate, dtype=float)
    q = np.asarray(dividend_yield, dtype=float)

    discounted_spot = S * np.exp(-q * tau)
    discounted_strike = K * np.exp(-r * tau)
    call = np.maximum(discounted_spot - discounted_strike, 0.0)
    put = np.maximum(discounted_strike - discounted_spot, 0.0)
    return np.where(np.asarray(is_call, dtype=bool), call, put)


def upper_bound(
    spot: ArrayLike,
    strike: ArrayLike,
    days_to_expiry: ArrayLike,
    rate: ArrayLike,
    dividend_yield: ArrayLike,
    is_call: ArrayLike,
    params: BsmInputs,
) -> np.ndarray:
    """No-arbitrage upper bound: ``S*exp(-q*tau)`` for a call, ``K*exp(-r*tau)`` for a put."""
    S = np.maximum(np.asarray(spot, dtype=float), params.min_price)
    K = np.maximum(np.asarray(strike, dtype=float), params.min_price)
    tau = tau_years(days_to_expiry, params)
    r = np.asarray(rate, dtype=float)
    q = np.asarray(dividend_yield, dtype=float)
    return np.where(np.asarray(is_call, dtype=bool), S * np.exp(-q * tau), K * np.exp(-r * tau))


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
) -> np.ndarray:
    """Black-Scholes-Merton price for an arbitrary basket, fully vectorised."""
    S = np.maximum(np.asarray(spot, dtype=float), params.min_price)
    K = np.maximum(np.asarray(strike, dtype=float), params.min_price)
    tau = tau_years(days_to_expiry, params)
    r = np.asarray(rate, dtype=float)
    q = np.asarray(dividend_yield, dtype=float)
    parts = d1_d2(S, K, days_to_expiry, sigma, r, q, params)

    discounted_spot = S * np.exp(-q * tau)
    discounted_strike = K * np.exp(-r * tau)
    call = discounted_spot * parts.n_d1 - discounted_strike * parts.n_d2
    put = discounted_strike * parts.n_minus_d2 - discounted_spot * parts.n_minus_d1
    return np.where(np.asarray(is_call, dtype=bool), call, put)


class Greeks(NamedTuple):
    """Per-contract greeks, unrounded.

    ``vega`` is per one volatility point (1% = 0.01 of sigma) and ``theta`` is per
    calendar day, per ``numerics.bsm``. Rounding is left to whatever displays them
    — rounding here destroys gamma on long-dated contracts.
    """

    delta: np.ndarray
    gamma: np.ndarray
    vega: np.ndarray
    theta: np.ndarray
    rho: np.ndarray


def bsm_vega(
    spot: ArrayLike,
    strike: ArrayLike,
    days_to_expiry: ArrayLike,
    sigma: ArrayLike,
    rate: ArrayLike,
    dividend_yield: ArrayLike,
    params: BsmInputs,
) -> np.ndarray:
    """Vega alone, per one volatility point.

    Vega is identical for a call and a put at the same strike, so no right is
    taken. Split out from :func:`bsm_greeks` because the implied-volatility
    solver needs vega on every iteration and nothing else — computing delta,
    gamma, theta and rho alongside it, only to discard them, is most of the work
    in the inner loop.
    """
    S = np.maximum(np.asarray(spot, dtype=float), params.min_price)
    K = np.maximum(np.asarray(strike, dtype=float), params.min_price)
    tau = tau_years(days_to_expiry, params)
    vol = np.clip(np.asarray(sigma, dtype=float), params.min_sigma, params.max_sigma)
    r = np.asarray(rate, dtype=float)
    q = np.asarray(dividend_yield, dtype=float)

    parts = d1_d2(S, K, days_to_expiry, vol, r, q, params)
    return (
        S * np.exp(-q * tau) * np.sqrt(tau) * norm_pdf(parts.d1)
    ) / params.vega_per_point_divisor


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
    """Vectorised BSM greeks. Per-contract spot and rate are supported."""
    S = np.maximum(np.asarray(spot, dtype=float), params.min_price)
    K = np.maximum(np.asarray(strike, dtype=float), params.min_price)
    tau = tau_years(days_to_expiry, params)
    vol = np.clip(np.asarray(sigma, dtype=float), params.min_sigma, params.max_sigma)
    r = np.asarray(rate, dtype=float)
    q = np.asarray(dividend_yield, dtype=float)
    calls = np.asarray(is_call, dtype=bool)

    parts = d1_d2(S, K, days_to_expiry, vol, r, q, params)
    root_tau = np.sqrt(tau)
    pdf_d1 = norm_pdf(parts.d1)
    disc_q = np.exp(-q * tau)
    disc_r = np.exp(-r * tau)

    delta = np.where(calls, disc_q * parts.n_d1, disc_q * (parts.n_d1 - 1.0))
    gamma = (disc_q * pdf_d1) / (S * vol * root_tau)
    vega = (S * disc_q * root_tau * pdf_d1) / params.vega_per_point_divisor

    theta_common = -(S * disc_q * pdf_d1 * vol) / (2.0 * root_tau)
    theta_call = theta_common - r * K * disc_r * parts.n_d2 + q * S * disc_q * parts.n_d1
    theta_put = theta_common + r * K * disc_r * parts.n_minus_d2 - q * S * disc_q * parts.n_minus_d1
    theta = np.where(calls, theta_call, theta_put) / params.theta_per_day_divisor

    rho_call = K * tau * disc_r * parts.n_d2 / params.vega_per_point_divisor
    rho_put = -K * tau * disc_r * parts.n_minus_d2 / params.vega_per_point_divisor
    rho = np.where(calls, rho_call, rho_put)

    return Greeks(delta=delta, gamma=gamma, vega=vega, theta=theta, rho=rho)


# ─────────────────────────────  implied volatility  ─────────────────────────────

#: Reasons a contract has no implied volatility. Reported per contract.
REASON_OK = "ok"
REASON_BELOW_INTRINSIC = "below_intrinsic"
REASON_ABOVE_UPPER_BOUND = "above_upper_bound"
REASON_NON_POSITIVE_PRICE = "non_positive_price"
REASON_NO_BRACKET = "no_bracket_in_sigma_bounds"
REASON_NOT_CONVERGED = "not_converged"
REASON_PRICE_INSENSITIVE = "price_insensitive_to_vol"


@dataclass(frozen=True)
class ImpliedVolResult:
    """Solver output.

    ``sigma`` is ``nan`` wherever ``converged`` is False. There is deliberately no
    way to read a non-converged value as if it were an implied volatility — that
    was the original defect.
    """

    sigma: np.ndarray
    converged: np.ndarray
    reason: np.ndarray
    newton_iterations: int
    bisection_iterations: int

    @property
    def converged_count(self) -> int:
        return int(np.count_nonzero(self.converged))

    @property
    def all_converged(self) -> bool:
        return bool(np.all(self.converged))

    def reason_counts(self) -> dict[str, int]:
        values, counts = np.unique(self.reason, return_counts=True)
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
) -> np.ndarray:
    """Brenner-Subrahmanyam closed-form seed: ``sigma ~ sqrt(2*pi/tau) * C / F``.

    Exact at the money. Away from it the bare formula reads low, so the moneyness
    term of the Manaster-Koehler seed is folded in:
    ``sigma_0 = sqrt(sigma_BS**2 + 2*|log(F/K)|/tau)``. The composite reduces to
    Brenner-Subrahmanyam at the money and to Manaster-Koehler deep out of it,
    which keeps the seed inside the bracket the solver needs.

    Where the formula is unusable (non-positive forward or price) the configured
    fixed seed is used for that contract only.
    """
    P = np.asarray(price, dtype=float)
    S = np.maximum(np.asarray(spot, dtype=float), params.min_price)
    K = np.maximum(np.asarray(strike, dtype=float), params.min_price)
    tau = tau_years(days_to_expiry, params)
    r = np.asarray(rate, dtype=float)
    q = np.asarray(dividend_yield, dtype=float)

    # Undiscount to the forward measure so calls and puts share one expression.
    forward = S * np.exp((r - q) * tau)
    undiscounted = P * np.exp(r * tau)

    with np.errstate(divide="ignore", invalid="ignore"):
        seed = np.sqrt(2.0 * np.pi / tau) * (undiscounted / forward)

    # Away from the money the formula reads low; nudge by the moneyness gap so the
    # seed does not start below a bracket it needs to be inside.
    with np.errstate(divide="ignore", invalid="ignore"):
        moneyness = np.abs(np.log(forward / K))
    seed = np.sqrt(np.maximum(seed * seed + 2.0 * moneyness / tau, 0.0))

    usable = np.isfinite(seed) & (seed > 0.0) & (forward > 0.0) & (P > 0.0)
    seed = np.where(usable, seed, solver.seed_fixed_sigma)
    return np.clip(seed, solver.seed_min_sigma, solver.seed_max_sigma)


def to_out_of_the_money(
    price: np.ndarray,
    spot: np.ndarray,
    strike: np.ndarray,
    days_to_expiry: np.ndarray,
    rate: np.ndarray,
    dividend_yield: np.ndarray,
    is_call: np.ndarray,
    params: BsmInputs,
) -> tuple[np.ndarray, np.ndarray]:
    """Convert every in-the-money contract to its out-of-the-money twin by parity.

    ``C - P = S*exp(-q*tau) - K*exp(-r*tau)``, and both sides of a strike share one
    implied volatility under Black-Scholes. Solving on the out-of-the-money side
    means the price being matched *is* the time value, which is the only part of
    the quote that responds to volatility at all.

    Returns ``(converted_price, converted_is_call)``.
    """
    tau = tau_years(days_to_expiry, params)
    forward_gap = spot * np.exp(-dividend_yield * tau) - strike * np.exp(-rate * tau)

    # A call is in the money when the discounted spot exceeds the discounted
    # strike; a put is in the money when it does not.
    itm = np.where(is_call, forward_gap > 0.0, forward_gap < 0.0)

    # call -> put subtracts the gap; put -> call adds it.
    converted = np.where(is_call, price - forward_gap, price + forward_gap)
    out_price = np.where(itm, converted, price)
    out_is_call = np.where(itm, ~is_call, is_call)

    # Parity can push a quote a hair below zero on rounding; it is zero time value.
    return np.maximum(out_price, 0.0), out_is_call


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

    Newton-Raphson from a Brenner-Subrahmanyam seed, then bisection on
    ``[min_sigma, max_sigma]`` for whatever Newton did not solve. Prices below
    discounted intrinsic or above the no-arbitrage upper bound are rejected before
    either runs.

    ``price`` is the option's market price (mid, normally) in the same units as
    ``spot`` and ``strike``. Rates and the returned sigma are decimals.
    """
    P = np.atleast_1d(np.asarray(price, dtype=float))
    n = P.size
    if n == 0:
        empty_f = np.array([], dtype=float)
        return ImpliedVolResult(
            sigma=empty_f,
            converged=np.array([], dtype=bool),
            reason=np.array([], dtype=object),
            newton_iterations=0,
            bisection_iterations=0,
        )

    S = np.broadcast_to(np.asarray(spot, dtype=float), (n,)).astype(float)
    K = np.broadcast_to(np.asarray(strike, dtype=float), (n,)).astype(float)
    D = np.broadcast_to(np.asarray(days_to_expiry, dtype=float), (n,)).astype(float)
    r = np.broadcast_to(np.asarray(rate, dtype=float), (n,)).astype(float)
    q = np.broadcast_to(np.asarray(dividend_yield, dtype=float), (n,)).astype(float)
    calls = np.broadcast_to(np.asarray(is_call, dtype=bool), (n,)).astype(bool)

    sigma = np.full(n, np.nan, dtype=float)
    converged = np.zeros(n, dtype=bool)
    reason = np.full(n, REASON_NOT_CONVERGED, dtype=object)

    # ── admissibility, before any iteration ──
    live = np.isfinite(P) & (P > 0.0)
    reason[~live] = REASON_NON_POSITIVE_PRICE

    if solver.reject_below_intrinsic:
        floor = intrinsic_value(S, K, D, r, q, calls, params)
        below = live & (floor - solver.price_abs_tolerance > P)
        reason[below] = REASON_BELOW_INTRINSIC
        live &= ~below

    if solver.reject_above_upper_bound:
        ceiling = upper_bound(S, K, D, r, q, calls, params)
        above = live & (ceiling + solver.price_abs_tolerance < P)
        reason[above] = REASON_ABOVE_UPPER_BOUND
        live &= ~above

    if not np.any(live):
        return ImpliedVolResult(sigma, converged, reason, 0, 0)

    # Solve on the out-of-the-money side of every strike. See to_out_of_the_money:
    # an in-the-money quote is almost entirely intrinsic value, which carries no
    # volatility information, so matching it to a price-scaled tolerance solves the
    # wrong quantity.
    target, target_is_call = to_out_of_the_money(P, S, K, D, r, q, calls, params)

    tolerance = np.maximum(solver.price_abs_tolerance, solver.price_rel_tolerance * np.abs(target))

    def price_at(vol: np.ndarray, mask: np.ndarray) -> np.ndarray:
        return bsm_price(
            S[mask], K[mask], D[mask], vol, r[mask], q[mask], target_is_call[mask], params
        )

    # ── Newton-Raphson ──
    if solver.seed_method == "brenner_subrahmanyam":
        seed = brenner_subrahmanyam_seed(target, S, K, D, r, q, params, solver)
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
        modelled = price_at(vol, idx)
        diff = modelled - target[idx]

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

        # bsm_vega reports vega per volatility point; Newton needs d(price)/d(sigma).
        dprice_dsigma = (
            bsm_vega(S[idx], K[idx], D[idx], vol, r[idx], q[idx], params)
            * params.vega_per_point_divisor
        )

        # Where vega has collapsed Newton is unusable. Leave those to bisection.
        usable = dprice_dsigma > solver.min_vega
        stalled = idx[~usable]
        active[stalled] = False

        movers = idx[usable]
        if movers.size:
            step_size = diff[usable] / dprice_dsigma[usable]
            working[movers] = np.clip(
                working[movers] - step_size, params.min_sigma, params.max_sigma
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

        # BSM price is monotonically increasing in sigma, so a root exists only
        # when the bounds straddle the market price. If they do not, the price is
        # outside the configured sigma range and there is nothing to report.
        bracketed = (f_lo <= 0.0) & (f_hi >= 0.0)
        unbracketed = idx[~bracketed]
        reason[unbracketed] = REASON_NO_BRACKET

        idx = idx[bracketed]
        lo, hi = lo[bracketed], hi[bracketed]
        if idx.size:
            for step in range(solver.max_bisection_iterations):
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
                if not np.any(keep):
                    break
                go_up = f_mid < 0.0
                lo = np.where(go_up, mid, lo)
                hi = np.where(go_up, hi, mid)
                idx, lo, hi = idx[keep], lo[keep], hi[keep]

    # ── post-solve sanity: did the price carry any volatility information? ──
    # A tolerance is met trivially when vega is near zero: far out of the money a
    # 1e-15 price is consistent with a wide band of sigmas, and the solver would
    # otherwise report whichever one it happened to land on as if it were data.
    solved = np.flatnonzero(converged)
    if solved.size:
        vega_per_point = bsm_vega(
            S[solved], K[solved], D[solved], sigma[solved], r[solved], q[solved], params
        )
        insensitive = vega_per_point < solver.min_vega_for_reported_iv
        if np.any(insensitive):
            dead = solved[insensitive]
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
