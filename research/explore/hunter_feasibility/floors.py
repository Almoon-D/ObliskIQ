"""The universe floors computed from the account, and how many US stocks pass them.

Exploration (DECISIONS D64): outside the coverage floor and never evidence of an effect. It
reads ``universe.json`` (``universe.py``) and the account's own configuration, and counts stocks;
it computes no return.

A stock passes, for a book of ``names`` positions of ``capital * share / names`` each, when
- its median daily traded value over the last sessions is at least the position over
  ``thesis.liquidity.max_share_of_daily_value`` (the book is at most that share of a day), and
- one leg's commission on IBKR's fixed US schedule (``costs.commissions`` row ``us_stock``) is
  at most the spread the cost model already charges on that leg in the stock's tier
  (``costs.friction.equity``: liquid from ``liquid_market_cap_usd``).

Usage: python -I research/explore/hunter_feasibility/floors.py <universe.json> <eurusd> <repo>
"""

import json
import statistics
import sys
from pathlib import Path

import yaml

CAPITALS_EUR = (10_000, 50_000, 250_000)
NAMES = (20, 40, 80)
#: The Hunter's share of managed capital: 50 % on a sufficient test, then up to all of it
#: (DECISIONS D149, D155).
SHARES = (0.5, 1.0)
#: Size tiers by market capitalisation in USD: micro below 300 million, small to 2 billion, mid
#: to the cost model's liquid bound (10 billion), large above.
TIERS = (("micro", 0, 3e8), ("small", 3e8, 2e9), ("mid", 2e9, 1e10), ("large", 1e10, float("inf")))


def main(path: Path, eurusd: float, repo: Path) -> None:
    costs = yaml.safe_load((repo / "config/defaults/costs.yaml").read_text())["costs"]
    thesis = yaml.safe_load((repo / "config/defaults/thesis.yaml").read_text())["thesis"]
    row = next(r for r in costs["commissions"] if r["id"] == "us_stock")
    rate, minimum, cap = (float(row[k]) for k in ("rate", "minimum", "maximum_fraction_of_value"))
    friction = costs["friction"]["equity"]
    liquid_cap = float(friction["liquid_market_cap_usd"])
    share_of_day = float(thesis["liquidity"]["max_share_of_daily_value"])
    stocks = [
        v
        for v in json.loads(path.read_text()).values()
        if v["bars"] and "median_value" in v["bars"] and v["market_cap"]
    ]

    def commission(position: float, price: float) -> float:
        shares = int(position // price)
        if shares == 0:
            return float("inf")
        value = shares * price
        return max(minimum, min(rate * shares, cap * value)) / value

    def spread(cap_usd: float) -> float:
        key = "liquid_spread_fraction" if cap_usd >= liquid_cap else "illiquid_spread_fraction"
        return float(friction[key])

    tiers = {
        name: [s for s in stocks if low <= s["market_cap"] < high] for name, low, high in TIERS
    }
    out = {
        "stocks": len(stocks),
        "eurusd": eurusd,
        "share_of_day": share_of_day,
        "all": {n: len(v) for n, v in tiers.items()},
        "rows": [],
    }
    for capital in CAPITALS_EUR:
        for share in SHARES:
            for names in NAMES:
                position = capital * share / names * eurusd
                floor = position / share_of_day
                passing = {
                    n: [
                        s
                        for s in v
                        if s["bars"]["median_value"] >= floor
                        and commission(position, s["bars"]["close"]) <= spread(s["market_cap"])
                    ]
                    for n, v in tiers.items()
                }
                medians = {
                    n: commission(position, statistics.median(s["bars"]["close"] for s in v))
                    for n, v in passing.items()
                    if v
                }
                out["rows"].append(
                    {
                        "capital_eur": capital,
                        "share": share,
                        "names": names,
                        "position_usd": round(position),
                        "daily_value_floor_usd": round(floor),
                        "commission_at_min": minimum / position,
                        "commission_at_tier_median_price": medians,
                        "passing": {n: len(v) for n, v in passing.items()},
                        "passing_total": sum(len(v) for v in passing.values()),
                    }
                )
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main(Path(sys.argv[1]), float(sys.argv[2]), Path(sys.argv[3]))
