"""Independent observations and the effect each hypothesis could detect at power 0.8, from the
audit's measurements (exploration, DECISIONS D64; the formulas of D62 and D63).

Each test is one of two in its confirmation run (the hypothesis and H5 beside it), so each
interval is at level 1 - 2 * 0.05 / 2 and the effect detectable at power p is
(z(1 - 0.05 / 2) + z(p)) * se. Observations are counted as D63 states: n names in a period whose
outcomes correlate on average by rho carry n / (1 + (n - 1) rho), summed over non-overlapping
periods.

- H2: events in non-overlapping half-years (a six-month horizon); the standard deviation and rho
  of six-month excess returns measured on the sampled stocks (coverage.py), all events counted
  as if every one had data, then only the matched ones.
- H4: one Fama-MacBeth observation a month from the first month with XBRL fundamentals for every
  filer; the top group's monthly excess standard deviation is not measured here (no
  survivorship-free stock panel exists in free data), so a stated range is used.
- H1: one observation a month in the confirmation sample; the standard deviation of a random third
  of the members against all of them is measured (msci.py) and is a floor, since a signal's top
  group also carries the signal's own tilt.

Usage: python -I research/explore/selection_audit/power.py <out dir>
"""

import json
import math
import sys
from collections import Counter
from pathlib import Path
from statistics import NormalDist

ALPHA = 0.05
TESTS = 2
POWER = 0.8
NORMAL = NormalDist()
Z = NORMAL.inv_cdf(1 - ALPHA / TESTS) + NORMAL.inv_cdf(POWER)
#: H4: XBRL fundamentals for every filer from fiscal periods after 15 June 2011, through the
#: last complete month.
H4_MONTHS = (2026 - 2011) * 12 + (9 - 7) + 1
H4_SD_RANGE = (0.015, 0.03)
#: H1's confirmation sample (2008-02 to 2026-09) and the standard deviation it declares.
H1_MONTHS = 224
H1_DECLARED_SD = 0.015


def effective(counts: list[int], rho: float) -> float:
    return sum(n / (1 + (n - 1) * rho) for n in counts if n > 0)


def main(out_dir: Path) -> None:
    coverage = json.loads((out_dir / "coverage.json").read_text())
    events = json.loads((out_dir / "events.json").read_text())
    msci = json.loads((out_dir / "msci-summary.json").read_text())
    spread = coverage["dispersion"]
    sd, rho = spread["sd_six_month_excess"], spread["mean_pairwise_correlation"]
    halves = Counter(f"{e['filed'][:4]}H{1 + (int(e['filed'][5:7]) > 6)}" for e in events)
    matched_share = {
        year: (entry.get("matched", 0) + entry.get("split_factor", 0)) / entry["n"]
        for year, entry in coverage["h2"].items()
    }
    all_counts = list(halves.values())
    matched_counts = [round(n * matched_share.get(half[:4], 0.0)) for half, n in halves.items()]
    result: dict[str, object] = {"z": Z}
    for label, counts in (("h2_all", all_counts), ("h2_matched", matched_counts)):
        n_eff = effective(counts, rho)
        se = sd / math.sqrt(n_eff)
        result[label] = {
            "half_years": len(counts),
            "events": sum(counts),
            "n_eff": n_eff,
            "se_six_months": se,
            "detectable_six_months": Z * se,
        }
    result["h4"] = {
        "months": H4_MONTHS,
        "detectable_a_year": [Z * s * 12 / math.sqrt(H4_MONTHS) for s in H4_SD_RANGE],
    }
    floor = msci["random_third_excess_sd_monthly"]
    members = msci["members_first_and_last"][1]
    result["h1"] = {
        "months": H1_MONTHS,
        "members": members,
        "n_eff_per_month": members / (1 + (members - 1) * msci["mean_pairwise_correlation"]),
        "detectable_a_year_at_floor": Z * floor * 12 / math.sqrt(H1_MONTHS),
        "detectable_a_year_declared": Z * H1_DECLARED_SD * 12 / math.sqrt(H1_MONTHS),
    }
    print(json.dumps(result, indent=1))
    (out_dir / "power.json").write_text(json.dumps(result))


if __name__ == "__main__":
    main(Path(sys.argv[1]))
