"""Session 4e, Step 3: the standard deviations a month that core-risk-scaling's power calculation
assumes before any data (exploration, DECISIONS D64; never evidence).

Everything here comes from the declared long-run matrix of Session 4c (the pre-registration's
`study.core.risk`), A's static weights computed from it by the harness, each base's declared
share, and one stated assumption: a total-risk strategy's risky share varies around its declared
share with a standard deviation of SHARE_SD, independently of the returns. No series is read.

    uv run python research/explore/risk_scaling_power/power.py <pre-registration>

For a candidate S against A, the yearly variance of the growth difference is the tracking
variance of S's average weights (its declared share of its base) against A's under the declared
matrix, plus (SHARE_SD x the base's declared volatility)^2; against its declared twin, only the
second term. A month's standard deviation is the yearly one over sqrt(12).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import math
import sys
from pathlib import Path

import numpy as np

from obliskiq.config.loader import load_config
from obliskiq.env import Environment
from obliskiq.quant.risk_budget import covariance_from
from obliskiq.research.prereg import TotalRiskSpec, load_prereg
from obliskiq.research.strategies import declared_share, static_weights, total_risk

#: The assumed standard deviation of a risky share around its declared value.
SHARE_SD = 0.15
MONTHS = 12


def main() -> int:
    prereg = load_prereg(Path(sys.argv[1]))
    settings = load_config(Environment().resolved_config_dir()).settings
    study = prereg.spec.study
    core = study.core
    roles = core.risk_roles()
    vols, corr = core.risk.matrix(roles)
    sigma = covariance_from(vols, corr)
    reference = static_weights(study.reference, core, settings)

    def vector(weights: dict) -> np.ndarray:
        return np.array([float(weights.get(role, 0)) for role in roles])

    a = vector(reference)
    print(f"A's declared volatility {math.sqrt(a @ sigma @ a):.4f}")
    for spec in [*study.candidates, *study.twins]:
        assert isinstance(spec, TotalRiskSpec)
        rule = total_risk(spec, core)
        share = float(declared_share(spec, core))
        base_vol = rule.declared_volatility()
        average = vector({role: share * float(w) for role, w in spec.base.items()})
        gap = average - a
        static = float(gap @ sigma @ gap)
        dynamic = (SHARE_SD * base_vol) ** 2
        print(
            f"{spec.name:<12} share {share:.4f} base vol {base_vol:.4f} "
            f"vs A: {math.sqrt(static + dynamic) / math.sqrt(MONTHS):.4f} a month "
            f"(static {math.sqrt(static):.4f}, dynamic {math.sqrt(dynamic):.4f} a year); "
            f"vs twin: {math.sqrt(dynamic) / math.sqrt(MONTHS):.4f} a month"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
