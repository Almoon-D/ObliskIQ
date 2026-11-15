"""Calibration for sizing, and component weights by empirical Bayes
(research/preregistrations/scoreboard.md; MASTER_PLAN §5.11, §5.5 "Learning").

**Calibration.** A layer that states probabilities is *calibrated* while its graded record has
the minimum of cohorts and effective observations and a Brier skill above zero against its base
rate; it is uncalibrated when either fails, and the state is re-read at every grading, so it can
switch back. Uncalibrated, a stated probability sizes nothing (the fixed risk budget). Calibrated,
``p_eff = p_base + shrink * (p_stated - p_base)`` where ``shrink`` is the one-sided lower bound of
the reliability slope ``β`` in ``o - p_base = β (p_stated - p_base) + ε`` (least squares through
the origin, its standard error scaled by ``√(n / N_eff)``), clipped to [0, 1]: the share of the
stated edge the outcomes bore out, kept only as far as the record shows it with confidence
(legacy ``calibration_state`` kept a fixed half).

**Weights.** With at least the minimum number of components past their own minimum sample, the
prior variance of their graded means is ``τ² = max(0, mean(m²) - mean(s²))`` (prior mean zero) and
each mean shrinks to ``m * τ² / (τ² + s²)``; a component's weight is its positive shrunk mean over
the sum of them. Without such evidence the registration's weights stand. A weight set is
versioned by the SHA-256 of its canonical JSON.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from statistics import NormalDist

from ..research.prereg import CalibrationSpec, WeightsSpec
from ..research.registry import canonical_sha256
from .grading import GradedCall, Record, cohorts, effective_observations, probability_score
from .rules import GradingRules

_NORMAL = NormalDist()


@dataclass(frozen=True, slots=True)
class CalibrationState:
    """Whether a layer's stated probabilities may size, and the share of their edge believed.

    ``shrink`` is 0 while uncalibrated: ``p_eff`` is then the base rate."""

    layer: str
    calibrated: bool
    shrink: float
    graded: int
    cohorts: int
    effective: float
    skill: float | None
    slope: float | None
    reason: str

    def p_eff(self, stated: float, base: float) -> float:
        """The probability sizing may use: the base rate plus the believed share of the stated
        edge."""
        if not (0 <= stated <= 1 and 0 <= base <= 1):
            raise ValueError(f"probabilities lie in [0, 1], got {stated} and {base}")
        return base + self.shrink * (stated - base)


def _slope_lower_bound(
    stated: Sequence[GradedCall], *, effective: float, alpha: float
) -> tuple[float | None, float | None]:
    """The slope through the origin of ``o - p_base`` on ``p_stated - p_base``, and its one-sided
    ``1 - alpha`` lower bound with the standard error scaled for dependence."""
    xs: list[float] = []
    ys: list[float] = []
    climatology = sum(1 for g in stated if g.right) / len(stated)
    for item in stated:
        base = item.call.base_rate if item.call.base_rate is not None else climatology
        xs.append(float(item.call.probability or 0.0) - base)
        ys.append((1.0 if item.right else 0.0) - base)
    sxx = sum(x * x for x in xs)
    n = len(xs)
    if sxx == 0 or n < 2 or effective <= 0:
        return None, None
    slope = sum(x * y for x, y in zip(xs, ys, strict=True)) / sxx
    residual = sum((y - slope * x) ** 2 for x, y in zip(xs, ys, strict=True)) / (n - 1)
    se = math.sqrt(residual / sxx) * math.sqrt(n / effective)
    return slope, slope - _NORMAL.inv_cdf(1 - alpha) * se


def calibration_state(
    layer: str, graded: Sequence[GradedCall], rules: GradingRules, spec: CalibrationSpec
) -> CalibrationState:
    """The calibration of ``layer`` from its graded calls that state a probability."""
    score = probability_score(graded, rules)
    stated = [g for g in graded if g.graded and g.call.probability is not None]

    def state(
        calibrated: bool, shrink: float, slope: float | None, reason: str
    ) -> CalibrationState:
        return CalibrationState(
            layer=layer,
            calibrated=calibrated,
            shrink=shrink,
            graded=score.count,
            cohorts=score.cohorts,
            effective=score.effective,
            skill=score.skill,
            slope=slope,
            reason=reason,
        )

    if score.cohorts < spec.min_cohorts or score.effective < float(spec.min_effective):
        return state(
            False,
            0.0,
            None,
            f"{score.count} graded probabilities in {score.cohorts} cohorts carry "
            f"{score.effective:.1f} effective observations; calibration needs "
            f"{spec.min_cohorts} cohorts and {spec.min_effective}",
        )
    if score.skill is None or score.skill <= float(spec.brier_skill_above):
        shown = "undefined" if score.skill is None else f"{score.skill:+.3f}"
        return state(
            False, 0.0, None, f"Brier skill {shown} against {score.base} is not above zero"
        )
    slope, lower = _slope_lower_bound(
        stated, effective=score.effective, alpha=float(spec.shrink_alpha)
    )
    if lower is None:
        return state(False, 0.0, slope, "the stated probabilities never differ from the base rate")
    shrink = min(1.0, max(0.0, lower))
    return state(
        True,
        shrink,
        slope,
        f"Brier skill {score.skill:+.3f} against {score.base} on {score.effective:.1f} effective "
        f"observations; the outcomes bore out {slope:.2f} of the stated edge, "
        f"{shrink:.2f} with confidence",
    )


# ── component weights ──


@dataclass(frozen=True, slots=True)
class WeightUpdate:
    """A layer's component weights: ``weights`` is ``None`` when the record moves nothing and the
    registration's weights stand (``reason`` says why); ``sha256`` versions a set."""

    layer: str
    weights: dict[str, float] | None
    shrunk: dict[str, float]
    tau2: float | None
    reason: str

    @property
    def sha256(self) -> str | None:
        return None if self.weights is None else canonical_sha256(self.weights)

    def as_json(self) -> str:
        return json.dumps(self.weights, sort_keys=True, separators=(",", ":"))


def _mature(item: Record, spec: WeightsSpec) -> bool:
    return (
        item.cohorts >= spec.min_cohorts
        and item.effective >= float(spec.min_effective)
        and item.estimate.mean is not None
        and item.estimate.se is not None
    )


def component_weights(
    layer: str, components: Mapping[str, Record], spec: WeightsSpec
) -> WeightUpdate:
    """Empirical-Bayes weights of ``layer``'s components from their forward records."""
    mature = {name: item for name, item in components.items() if _mature(item, spec)}
    if len(mature) < spec.min_components:
        return WeightUpdate(
            layer,
            None,
            {},
            None,
            f"{len(mature)} of {len(components)} components have the minimum sample; "
            f"weights move from {spec.min_components}",
        )
    means = {name: float(item.estimate.mean or 0.0) for name, item in mature.items()}
    variances = {name: float(item.estimate.se or 0.0) ** 2 for name, item in mature.items()}
    tau2 = max(
        0.0,
        sum(m * m for m in means.values()) / len(means) - sum(variances.values()) / len(variances),
    )
    shrunk = {
        name: (means[name] * tau2 / (tau2 + variances[name]) if tau2 + variances[name] > 0 else 0.0)
        for name in means
    }
    positive = {name: value for name, value in shrunk.items() if value > 0}
    if not positive:
        return WeightUpdate(
            layer,
            None,
            shrunk,
            tau2,
            "no component's shrunk mean is above zero: the registration's weights stand",
        )
    total = sum(positive.values())
    weights = {name: positive.get(name, 0.0) / total for name in sorted(components)}
    return WeightUpdate(
        layer, weights, shrunk, tau2, f"τ² = {tau2:.3g} over {len(mature)} components"
    )


def graded_effective(graded: Sequence[GradedCall], rules: GradingRules) -> float:
    """The effective observations of a set of graded calls."""
    return effective_observations(
        [len(items) for items in cohorts(graded).values()],
        rho=rules.rho,
        horizon_weeks=rules.horizon_weeks,
    )
