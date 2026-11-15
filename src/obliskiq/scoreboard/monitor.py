"""Monitoring of every active layer against its twin (DECISIONS D65; MASTER_PLAN §5.19;
research/preregistrations/scoreboard.md "Monitoring").

Each active layer has a **twin**: its own signals on the same dates, simulated with the cost model.
At each month end from activation the layer's engine records the month: the live log return, the
twin's and the reference's (``store/scoreboard.layer_months``). From the first look month to the
last planned month, every month end is a look at two one-sided boundaries:

* **the gap**, live less twin: ``t = (ḡ + tolerance) / (s / √n)`` against Student's t with
  ``n - 1`` degrees of freedom. Below its boundary the layer's new entries freeze, an alert goes
  out and an operational investigation opens; the owner lifts the freeze after it.
* **the twin** against the backtest's predictive distribution for the months elapsed:
  ``z = (S - n mu_b) / sqrt(n sigma_b^2 + n^2 se_b^2)`` with ``S`` the twin's summed excess over its
  reference. Below its boundary the layer returns to shadow at zero weight, an alert goes out and
  a research review opens.

Each boundary spends its alpha over the planned months by a Pocock-type function,
``alpha(t) = alpha ln(1 + (e - 1) t)``; a look spends the increase since the previous look, and
the looks are combined by a union bound, so the chance of any false alarm over every look is at
most alpha whatever the dependence between them. A look with a month that was not measured is recorded not
evaluable and changes nothing. Monitoring reduces risk; it never declares a verdict.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from statistics import NormalDist

from ..quant.inference import student_t_quantile
from ..research.prereg import MonitoringSpec
from ..store.scoreboard import Boundary, LayerMonth

_NORMAL = NormalDist()


def spent_by(alpha: float, t: float) -> float:
    """The Pocock-type spending function: the alpha spent by information fraction ``t``."""
    if not 0.0 <= t <= 1.0:
        raise ValueError(f"an information fraction lies in [0, 1], got {t}")
    return alpha * math.log(1.0 + (math.e - 1.0) * t)


def look_alpha(alpha: float, month: int, spec: MonitoringSpec) -> float:
    """The alpha the look at ``month`` (counted from activation) spends: the spending function's
    increase since the previous look (the first look spends everything up to it)."""
    first, planned = spec.first_look_month, spec.planned_months
    if not first <= month <= planned:
        raise ValueError(f"month {month} is not a look (looks run from {first} to {planned})")
    previous = 0 if month == first else month - 1
    return spent_by(alpha, month / planned) - spent_by(alpha, previous / planned)


def is_look(month: int, spec: MonitoringSpec) -> bool:
    return spec.first_look_month <= month <= spec.planned_months


@dataclass(frozen=True, slots=True)
class Backtest:
    """The confirmation run's monthly series of the layer's primary metric: its mean, standard
    deviation and the standard error of the mean."""

    mean: float
    sd: float
    se: float


@dataclass(frozen=True, slots=True)
class LookResult:
    """A look at one boundary: ``statistic`` and ``critical`` are ``None`` when not evaluable."""

    boundary: Boundary
    month: int
    statistic: float | None
    critical: float | None
    spent: float
    detail: str

    @property
    def evaluable(self) -> bool:
        return self.statistic is not None

    @property
    def breached(self) -> bool:
        return (
            self.statistic is not None
            and self.critical is not None
            and self.statistic <= self.critical
        )


def gap_look(months: Sequence[LayerMonth], spec: MonitoringSpec) -> LookResult:
    """The gap's look at the last of ``months`` (every month since activation, in order)."""
    n = len(months)
    alpha = look_alpha(float(spec.gap.alpha), n, spec)
    gaps: list[float] = []
    for month in months:
        if month.live is None or month.twin is None:
            return LookResult(
                "gap", n, None, None, alpha, f"{month.month} has no live or twin return"
            )
        gaps.append(month.live - month.twin)
    mean = sum(gaps) / n
    sd = math.sqrt(sum((g - mean) ** 2 for g in gaps) / (n - 1))
    if sd == 0:
        return LookResult("gap", n, None, None, alpha, "the gaps do not vary: no t statistic")
    tolerance = float(spec.gap.tolerance_per_month)
    statistic = (mean + tolerance) / (sd / math.sqrt(n))
    critical = -student_t_quantile(1.0 - alpha, n - 1)
    return LookResult(
        "gap",
        n,
        statistic,
        critical,
        alpha,
        f"live less twin {mean:+.4f} a month over {n} months (sd {sd:.4f}); "
        f"t {statistic:+.2f} against {critical:+.2f}",
    )


def twin_look(months: Sequence[LayerMonth], backtest: Backtest, spec: MonitoringSpec) -> LookResult:
    """The twin's look at the last of ``months`` against the backtest's predictive distribution
    for the months elapsed."""
    n = len(months)
    alpha = look_alpha(float(spec.twin.alpha), n, spec)
    excess: list[float] = []
    for month in months:
        if month.twin is None or month.reference is None:
            return LookResult(
                "twin", n, None, None, alpha, f"{month.month} has no twin or reference return"
            )
        excess.append(month.twin - month.reference)
    total = sum(excess)
    spread = math.sqrt(n * backtest.sd**2 + n**2 * backtest.se**2)
    if spread == 0:
        return LookResult("twin", n, None, None, alpha, "the backtest predicts no spread")
    statistic = (total - n * backtest.mean) / spread
    critical = -_NORMAL.inv_cdf(1.0 - alpha)
    return LookResult(
        "twin",
        n,
        statistic,
        critical,
        alpha,
        f"twin over reference {total:+.4f} in {n} months against {n * backtest.mean:+.4f} "
        f"predicted; z {statistic:+.2f} against {critical:+.2f}",
    )


def months_since(first: str, last: str) -> int:
    """Months from ``first`` to ``last`` (``YYYY-MM``) counted whole: the same month is 1."""
    a, b = int(first[:4]) * 12 + int(first[5:7]), int(last[:4]) * 12 + int(last[5:7])
    return b - a + 1
