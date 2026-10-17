"""The structure check: the trend tilt on the longest history there is, before tax
(MASTER_PLAN §5.4; Session 4c).

A pre-registration may ask how the tilt's constants behave over a century, where only pre-tax
monthly returns exist (an equity market and its one-month bill, the Ken French library from
1926). The check is descriptive: it is never an outcome, never counts in the Bonferroni
adjustment, and says nothing about tax.

Mechanics. Levels are the products of the monthly returns. At the end of each month from the
first one the tilt can evaluate, its scale (``quant.trend``, the core's constants) is the market
weight held through the next month, the rest in the bill; each month the drifted weight is traded
back to the scale, paying ``cost_per_leg`` on each of the two legs. Holding the market pays
nothing. The interval of the difference in annualised log growth comes from a moving-block
bootstrap of the two arms' monthly returns as they happened (paired), so it describes the
realised series, not the signal re-run on resampled markets.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Final

import numpy as np

from ..quant.trend import TrendRule, trend_state
from .decide import AdequacyCheck
from .prereg import BootstrapSpec, StructureSpec
from .stats import (
    block_bootstrap_indices,
    breach_probability,
    max_drawdown,
    percentile_interval,
    years_between,
)

#: Both legs of a rebalance trade the same value: the market leg and the bill leg.
LEGS: Final = 2


@dataclass(frozen=True, slots=True)
class StructureArm:
    log_growth: float
    max_drawdown: float
    breach: tuple[tuple[Decimal, float], ...]


@dataclass(frozen=True, slots=True)
class StructureSubperiod:
    start: date
    end: date
    growth_difference: float
    drawdown_difference: float


@dataclass(frozen=True, slots=True)
class StructureResult:
    """The tilt against holding before tax on the longest history (descriptive)."""

    spec: StructureSpec
    months: int
    first: date | None
    last: date | None
    checks: tuple[AdequacyCheck, ...]
    overlay: StructureArm | None
    held: StructureArm | None
    interval: tuple[float, float] | None
    months_reduced: int
    subperiods: tuple[StructureSubperiod, ...]


def overlay_returns(
    market: np.ndarray, cash: np.ndarray, rule: TrendRule, cost_per_leg: float
) -> tuple[np.ndarray, np.ndarray, int]:
    """The overlay's monthly returns from the first month after the tilt can first evaluate,
    the scales it held, and that first month's index."""
    market_level = np.cumprod(1 + market).tolist()
    cash_level = np.cumprod(1 + cash).tolist()
    start = rule.months_needed - 1
    scales = []
    for k in range(start, market.size - 1):
        state = trend_state(market_level[: k + 1], cash_level[: k + 1], rule)
        if state is None:  # pragma: no cover - start is where the history first suffices
            raise ValueError("the tilt cannot evaluate where its history first suffices")
        scales.append(state.scale)
    weights = np.array(scales)
    returns = []
    drifted = weights[0] if weights.size else 1.0
    for step, weight in enumerate(weights):
        k = start + 1 + step
        traded = abs(weight - drifted) if step else 0.0
        result = weight * market[k] + (1 - weight) * cash[k] - LEGS * cost_per_leg * traded
        returns.append(result)
        grown_market = weight * (1 + market[k])
        drifted = grown_market / (grown_market + (1 - weight) * (1 + cash[k]))
    return np.array(returns), weights, start


def _arm(
    returns: np.ndarray, worst: np.ndarray, years: float, limits: Sequence[Decimal]
) -> StructureArm:
    levels = np.concatenate([[1.0], np.cumprod(1 + returns)])
    return StructureArm(
        log_growth=float(np.log(levels[-1]) / years),
        max_drawdown=max_drawdown(levels.tolist()),
        breach=tuple((x, breach_probability(worst.tolist(), float(x))) for x in limits),
    )


def _path_drawdowns(log_returns: np.ndarray) -> np.ndarray:
    """The worst drawdown of each resampled path (rows), from a start at 1."""
    levels = np.exp(np.cumsum(log_returns, axis=1))
    padded = np.concatenate([np.ones((levels.shape[0], 1)), levels], axis=1)
    peaks = np.maximum.accumulate(padded, axis=1)[:, 1:]
    worst = np.asarray((levels / peaks - 1).min(axis=1), dtype=float)
    return np.asarray(np.minimum(worst, 0.0), dtype=float)


def structure_check(
    spec: StructureSpec,
    months: Sequence[date],
    market: Sequence[float],
    cash: Sequence[float],
    checks: tuple[AdequacyCheck, ...],
    *,
    rule: TrendRule,
    bootstrap: BootstrapSpec,
    limits: Sequence[Decimal],
    alpha: float,
    days_per_year: float,
) -> StructureResult:
    """Run the check on monthly returns dated by ``months`` (each passed check first)."""
    if not all(check.passed for check in checks):
        return StructureResult(spec, len(months), None, None, checks, None, None, None, 0, ())
    market_returns = np.asarray(market, dtype=float)
    cash_returns = np.asarray(cash, dtype=float)
    overlay, weights, start = overlay_returns(
        market_returns, cash_returns, rule, float(spec.cost_per_leg)
    )
    held = market_returns[start + 1 :]
    first, last = months[start], months[-1]
    years = years_between(first, last, days_per_year=days_per_year)
    paths = block_bootstrap_indices(
        held.size, block=bootstrap.block_months, n_boot=bootstrap.n_boot, seed=bootstrap.seed
    )
    log_overlay, log_held = np.log1p(overlay), np.log1p(held)
    gaps = (log_overlay[paths].sum(axis=1) - log_held[paths].sum(axis=1)) / years
    subperiods: list[StructureSubperiod] = []
    dated = list(months[start + 1 :])
    for sub_first, sub_last in spec.subperiods:
        mask = np.array([sub_first <= d <= sub_last for d in dated])
        if not mask.any():
            continue
        # Over the months it holds: from the month end before its first return to its last.
        held_months = np.flatnonzero(mask)
        span = years_between(
            months[start + held_months[0]],
            months[start + 1 + held_months[-1]],
            days_per_year=days_per_year,
        )
        part_overlay = np.concatenate([[1.0], np.cumprod(1 + overlay[mask])])
        part_held = np.concatenate([[1.0], np.cumprod(1 + held[mask])])
        subperiods.append(
            StructureSubperiod(
                sub_first,
                sub_last,
                float(np.log(part_overlay[-1] / part_held[-1]) / span),
                max_drawdown(part_overlay.tolist()) - max_drawdown(part_held.tolist()),
            )
        )
    return StructureResult(
        spec=spec,
        months=len(months),
        first=first,
        last=last,
        checks=checks,
        overlay=_arm(overlay, _path_drawdowns(log_overlay[paths]), years, limits),
        held=_arm(held, _path_drawdowns(log_held[paths]), years, limits),
        interval=percentile_interval(gaps.tolist(), alpha=alpha, tests=1),
        months_reduced=int((weights < 1).sum()),
        subperiods=tuple(subperiods),
    )
