"""MSCI country indices for H1: which of the point-in-time MSCI ACWI countries the free endpoint
serves, from when, and the dispersion that sizes a country-momentum test (exploration, D64).

The universe on each date is the countries in MSCI ACWI then (developed and emerging), from
MSCI's market classification history: the reclassifications MSCI lists on its "Market
Classification" page (read 2026-10-17) and the later ones in its annual reviews. Each country's
standard (large and mid cap) index is asked for in EUR, net total return, daily. A member whose
index the endpoint does not serve is lost to survivorship for the months it was a member.

For power only (never the signal): the cross-sectional standard deviation of monthly returns,
the mean pairwise correlation of monthly returns, and the standard deviation of a random third
of the members minus the equal-weighted universe (the excess return of a top group with no
signal), over many random draws.

Usage: python -I research/explore/selection_audit/msci.py <out dir>
"""

import json
import random
import sys
import urllib.request
from datetime import date
from itertools import pairwise
from pathlib import Path

import numpy as np

URL = (
    "https://app2.msci.com/products/service/index/indexmaster/getLevelDataForGraph"
    "?currency_symbol=EUR&index_variant=NETR&start_date=19970101&end_date=20261016"
    "&data_frequency=DAILY&index_codes={code}"
)
#: Every country in MSCI ACWI at some time since 2001: (name, MSCI code or None when the
#: endpoint's search lists no standard index, membership spans as (first month, last month)).
#: A span's month is the month after the review that changed it (effective at the close of the
#: review's last business day).
OPEN = "2026-09"
COUNTRIES: list[tuple[str, int | None, list[tuple[str, str]]]] = [
    ("Australia", 903600, [("2001-01", OPEN)]),
    ("Austria", 904000, [("2001-01", OPEN)]),
    ("Belgium", 905600, [("2001-01", OPEN)]),
    ("Canada", 912400, [("2001-01", OPEN)]),
    ("Denmark", 920800, [("2001-01", OPEN)]),
    ("Finland", 924600, [("2001-01", OPEN)]),
    ("France", 925000, [("2001-01", OPEN)]),
    ("Germany", 928000, [("2001-01", OPEN)]),
    ("Hong Kong", 934400, [("2001-01", OPEN)]),
    ("Ireland", 937200, [("2001-01", OPEN)]),
    ("Israel", 300400, [("2001-01", OPEN)]),
    ("Italy", 938000, [("2001-01", OPEN)]),
    ("Japan", 939200, [("2001-01", OPEN)]),
    ("Netherlands", 952800, [("2001-01", OPEN)]),
    ("New Zealand", 955400, [("2001-01", OPEN)]),
    ("Norway", 957800, [("2001-01", OPEN)]),
    ("Portugal", 962000, [("2001-01", OPEN)]),
    ("Singapore", 998100, [("2001-01", OPEN)]),
    ("Spain", 972400, [("2001-01", OPEN)]),
    ("Sweden", 975200, [("2001-01", OPEN)]),
    ("Switzerland", 975600, [("2001-01", OPEN)]),
    ("United Kingdom", 982600, [("2001-01", OPEN)]),
    ("USA", 984000, [("2001-01", OPEN)]),
    ("Brazil", 907600, [("2001-01", OPEN)]),
    ("Chile", 915200, [("2001-01", OPEN)]),
    ("China", 302400, [("2001-01", OPEN)]),
    ("Colombia", 917000, [("2001-01", OPEN)]),
    ("Czech Republic", 920000, [("2001-01", OPEN)]),
    ("Egypt", 105766, [("2001-01", OPEN)]),
    ("Greece", 930000, [("2001-01", OPEN)]),
    ("Hungary", 934800, [("2001-01", OPEN)]),
    ("India", 935600, [("2001-01", OPEN)]),
    ("Indonesia", 105767, [("2001-01", OPEN)]),
    ("Korea", 941000, [("2001-01", OPEN)]),
    ("Malaysia", 105768, [("2001-01", OPEN)]),
    ("Mexico", 848400, [("2001-01", OPEN)]),
    ("Peru", 960400, [("2001-01", OPEN)]),
    ("Philippines", 860800, [("2001-01", OPEN)]),
    ("Poland", 961600, [("2001-01", OPEN)]),
    ("South Africa", 971000, [("2001-01", OPEN)]),
    ("Taiwan", 915800, [("2001-01", OPEN)]),
    ("Thailand", 105769, [("2001-01", OPEN)]),
    ("Turkey", 979200, [("2001-01", OPEN)]),
    # Frontier to emerging in May 2014.
    ("Qatar", 133715, [("2014-06", OPEN)]),
    ("United Arab Emirates", 133717, [("2014-06", OPEN)]),
    # Standalone to emerging in two steps, May and August 2019.
    ("Saudi Arabia", 705405, [("2019-06", OPEN)]),
    # Frontier to emerging in November 2020.
    ("Kuwait", 133713, [("2020-12", OPEN)]),
    # Emerging to frontier in May 2009; emerging again from May 2019; standalone from
    # November 2021.
    ("Argentina", 903200, [("2001-01", "2009-05"), ("2019-06", "2021-11")]),
    # Removed from emerging in December 2008; emerging again from May 2017 to November 2021.
    ("Pakistan", 958600, [("2001-01", "2008-12"), ("2017-06", "2021-11")]),
    # Emerging to frontier in November 2008.
    ("Jordan", 940000, [("2001-01", "2008-11")]),
    # Emerging to frontier in November 2013.
    ("Morocco", 105765, [("2001-01", "2013-11")]),
    # Emerging to standalone in May 2006 (the index was discontinued in January 2008).
    ("Venezuela", None, [("2001-01", "2006-05")]),
    # Emerging to standalone, effective 9 March 2022 (a member through its review month).
    ("Russia", None, [("2001-01", "2022-03")]),
]
DRAWS = 2000
SEED = 20261017


def months(first: str, last: str) -> list[str]:
    year, month = map(int, first.split("-"))
    end = tuple(map(int, last.split("-")))
    out = []
    while (year, month) <= end:
        out.append(f"{year:04d}-{month:02d}")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return out


def fetch(code: int) -> list[tuple[date, float]]:
    with urllib.request.urlopen(URL.format(code=code), timeout=60) as response:
        data = json.load(response)
    levels = data.get("indexes", {}).get("INDEX_LEVELS", [])
    return [
        (
            date(r["calc_date"] // 10000, r["calc_date"] // 100 % 100, r["calc_date"] % 100),
            float(r["level_eod"]),
        )
        for r in levels
    ]


def main(out_dir: Path) -> None:
    cache = out_dir / "msci.json"
    if cache.exists():
        stored = json.loads(cache.read_text())
    else:
        stored = {}
        for name, code, _spans in COUNTRIES:
            stored[name] = [] if code is None else [(d.isoformat(), v) for d, v in fetch(code)]
        cache.write_text(json.dumps(stored))
    month_end: dict[str, dict[str, float]] = {}
    for name, rows in stored.items():
        last: dict[str, float] = {}
        for day, value in rows:
            last[day[:7]] = value
        month_end[name] = last
    calendar = months("2001-01", OPEN)
    members_by_month = {m: [] for m in calendar}
    for name, _code, spans in COUNTRIES:
        for first, last in spans:
            for m in months(first, last):
                members_by_month[m].append(name)
    lost_by_year: dict[str, list[float]] = {}
    for m in calendar:
        members = members_by_month[m]
        lost = [n for n in members if m not in month_end[n]]
        lost_by_year.setdefault(m[:4], []).append(len(lost) / len(members))
    print("served:", sum(1 for rows in stored.values() if rows), "of", len(stored))
    for name, rows in stored.items():
        if rows:
            print(f"  {name:22s} {rows[0][0]} .. {rows[-1][0]}  {len(rows)} days")
        else:
            print(f"  {name:22s} not served")
    print("share of members not served, by year (mean over months; members at year end):")
    for year, shares in lost_by_year.items():
        last = members_by_month.get(f"{year}-12", members_by_month[OPEN])
        print(f"  {year}  {np.mean(shares):.1%}  members {len(last)}")
    # Dispersion of monthly returns among served members (power inputs only).
    rows_by_month = []
    for previous, current in pairwise(calendar):
        returns = {}
        for name in members_by_month[previous]:
            prices = month_end[name]
            if previous in prices and current in prices:
                returns[name] = np.log(prices[current] / prices[previous])
        rows_by_month.append(returns)
    cross_sd = [np.std(list(r.values()), ddof=1) for r in rows_by_month if len(r) > 2]
    names = sorted(month_end)
    matrix = np.array([[r.get(n, np.nan) for n in names] for r in rows_by_month])
    pairs = []
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            both = ~np.isnan(matrix[:, i]) & ~np.isnan(matrix[:, j])
            if both.sum() >= 36:
                pairs.append(np.corrcoef(matrix[both, i], matrix[both, j])[0, 1])
    rng = random.Random(SEED)
    excess_sd = []
    for _ in range(DRAWS):
        series = []
        for r in rows_by_month:
            members = sorted(r)
            if len(members) < 3:
                continue
            third = rng.sample(members, max(1, len(members) // 3))
            simple = {n: np.exp(r[n]) - 1 for n in members}
            series.append(np.mean([simple[n] for n in third]) - np.mean(list(simple.values())))
        excess_sd.append(np.std(series, ddof=1))
    result = {
        "months": len(rows_by_month),
        "mean_cross_sectional_sd": float(np.mean(cross_sd)),
        "mean_pairwise_correlation": float(np.mean(pairs)),
        "random_third_excess_sd_monthly": float(np.mean(excess_sd)),
        "members_first_and_last": [len(members_by_month["2001-01"]), len(members_by_month[OPEN])],
        "lost_by_year": {y: float(np.mean(s)) for y, s in lost_by_year.items()},
    }
    print(json.dumps({k: v for k, v in result.items() if k != "lost_by_year"}, indent=1))
    (out_dir / "msci-summary.json").write_text(json.dumps(result))


if __name__ == "__main__":
    main(Path(sys.argv[1]))
