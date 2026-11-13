"""Power before data for the Hunter's fixed sequence (DECISIONS D154).

Exploration (DECISIONS D64): outside the coverage floor and never evidence of an effect. Every
input is a citation or the account's configuration, stated in
``research/reports/hunter-feasibility.md``; no return of any candidate is read.

For each test the effect is the book's after-tax log growth minus that of its own universe
bought and held. The net effect the literature supports is

    gross long-short premium in the source's own sample  x  what survives publication
    x  1/2 (a top group against the whole universe holds half a long-short spread)
    -  turnover x round-trip cost at the tier's cost tier
    -  tax: the excess is taxed at the savings rate, and the gains the book realises early lose
       the deferral the universe bought and held keeps

computed twice: central (McLean and Pontiff's post-publication decline) and generous (their
out-of-sample decline only, and a larger gross premium). Theta, the months and the tracking
error give the probability of a sufficient outcome, the power at theta and the effect detectable
at power 0.8, with one test at a time: a test runs only if the one before it was sufficient.

Usage: python -I research/explore/hunter_feasibility/power.py <repo>
"""

import json
import math
import sys
from pathlib import Path

import yaml

from obliskiq.research.stats import detectable_effect, power, sufficient_probability

ALPHA = 0.05
TARGET = 0.8
THETA = 0.02
#: The whole Sharadar span as one confirmation sample (January 1999 to September 2026; the first
#: year of prices and filings feeds the lookbacks), and the free XBRL span (July 2011 on).
MONTHS = {"sharadar": 332, "xbrl": 183}
#: McLean and Pontiff (2016): returns 26 % lower out of sample and 58 % lower after publication.
SURVIVES = {"central": 1 - 0.58, "generous": 1 - 0.26}
#: A top group against the whole universe holds half a long-short spread whose return is linear
#: in the score's rank.
LONG_ONLY = 0.5
#: Equity growth a year on which realised gains are taxed early, the horizon of a test, and the
#: Spanish savings rate at the 21 % band (config/jurisdictions/es.yaml).
EQUITY_GROWTH = 0.07
HORIZON_YEARS = 15
TAX = 0.21

#: Gross long-short premia a year in each source's own sample (in percent) and the one-way
#: turnover a year the registered holding period implies.
TESTS = [
    # name, tier, gross central, gross generous, turnover, tracking error low / high
    ("H12 composite, whole universe", "all", 5.70, 8.0, 0.9, 5.0, 9.0),
    ("H12, micro caps", "micro", 5.70, 8.0 * 1.5, 0.9, 5.0, 9.0),
    ("H12, small caps", "small", 5.70, 8.0 * 1.5, 0.9, 5.0, 9.0),
    ("H12, mid caps", "mid", 5.70, 8.0, 0.9, 5.0, 9.0),
    ("H12, large caps", "large", 5.70, 8.0, 0.9, 5.0, 9.0),
    ("H2 insider-purchase clusters", "all", 9.84, 9.84, 2.0, 9.0, 12.0),
    ("H3 spin-offs and deletions", "all", 0.0, 8.0, 0.5, 9.0, 12.0),
    ("H4 52-week high, momentum, profitability", "all", 7.55, 10.0, 1.75, 5.0, 9.0),
    ("H10 cheapness with quality", "all", 3.51, 6.0, 0.5, 5.0, 9.0),
    ("H11 post-earnings drift", "all", 0.0, 4.0, 4.0, 9.0, 12.0),
]


def round_trip(repo: Path) -> dict[str, float]:
    friction = yaml.safe_load((repo / "config/defaults/costs.yaml").read_text())["costs"][
        "friction"
    ]
    eq = friction["equity"]
    liquid = 2 * (float(eq["liquid_spread_fraction"]) + float(eq["liquid_slippage_fraction"]))
    illiquid = 2 * (float(eq["illiquid_spread_fraction"]) + float(eq["illiquid_slippage_fraction"]))
    # The whole universe's names by tier on 2026-11-13 (floors.py): 810 of 4,832 are large.
    large_share = 810 / 4832
    return {
        "micro": illiquid,
        "small": illiquid,
        "mid": illiquid,
        "large": liquid,
        "all": large_share * liquid + (1 - large_share) * illiquid,
    }


def deferral_drag(turnover: float) -> float:
    """The log growth a year lost by realising gains early, against holding to the horizon."""
    yearly = math.log1p(math.expm1(EQUITY_GROWTH) * (1 - TAX))
    held = math.log1p(math.expm1(EQUITY_GROWTH * HORIZON_YEARS) * (1 - TAX)) / HORIZON_YEARS
    return min(turnover, 1.0) * (held - yearly)


def main(repo: Path) -> None:
    costs = round_trip(repo)
    rows = []
    for name, tier, gross_c, gross_g, turnover, te_low, te_high in TESTS:
        effects = {}
        for case, gross in (("central", gross_c), ("generous", gross_g)):
            excess = gross / 100 * SURVIVES[case] * LONG_ONLY - turnover * costs[tier]
            excess -= deferral_drag(turnover)
            effects[case] = excess * (1 - TAX) if excess > 0 else excess
        row = {
            "test": name,
            "tier": tier,
            "round_trip": costs[tier],
            "turnover": turnover,
            "effect": effects,
            "by_sample": {},
        }
        for sample, months in MONTHS.items():
            out = {}
            for label, te in (("te_low", te_low), ("te_high", te_high)):
                se = te / 100 / math.sqrt(12) * 12 / math.sqrt(months)
                out[label] = {
                    "tracking_error": te / 100,
                    "se": se,
                    "power_at_theta": power(THETA, se, alpha=ALPHA, tests=1),
                    "detectable": detectable_effect(se, alpha=ALPHA, tests=1, target_power=TARGET),
                    "p_sufficient": {
                        c: sufficient_probability(e, THETA, se, alpha=ALPHA, tests=1)
                        for c, e in effects.items()
                    },
                }
            row["by_sample"][sample] = out
        rows.append(row)
    print(
        json.dumps(
            {
                "theta": THETA,
                "alpha": ALPHA,
                "deferral_full_turnover": deferral_drag(1),
                "rows": rows,
            },
            indent=1,
        )
    )


if __name__ == "__main__":
    main(Path(sys.argv[1]))
