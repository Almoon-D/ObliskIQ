"""Session 4e, Step 1: data checks of the series a total-risk study and its long-history check
read (exploration, DECISIONS D64; never evidence).

Run after `obliskiq research data load ...` (README.md) with the same OBLISK_DATA_DIR:

    uv run python research/explore/risk_scaling_data/checks.py > results/checks.json

Each series is checked for what SESSION_PROMPTS Session 4e's Step 1 names: its metadata (vendor,
currency, units, frequency, point in time), its span, duplicates and impossible values, its
continuity on the weekday calendar (weekdays without a row, the longest run of them, runs of an
unchanged value), and, for a daily series, how often it has no row on an MSCI calculation day (the
days a daily study's grid is built on). A level series is also scanned for a one-day change beyond
a splice threshold (a rebasing or a unit change leaves one), reported by date only. **No return,
volatility, correlation or drawdown is computed**: the choice of sources and span rests on these
checks alone.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import json
import math
import sys
from collections import Counter
from datetime import date, timedelta
from itertools import pairwise

from obliskiq.config.loader import load_config
from obliskiq.env import Environment
from obliskiq.store import open_research_store
from obliskiq.store.research import research_observations, research_series_info

#: A daily level that moves by more than this in log terms in one day is listed as a possible
#: splice (a rebasing or a unit change), for inspection; it is not a statistic of the series.
SPLICE_LOG_CHANGE = math.log(1.25)
#: A return series whose value lies outside these bounds is in the wrong units (percent).
RETURN_BOUNDS = (-0.95, 1.5)
#: A run of identical values this long on consecutive rows is reported as stale.
STALE_RUN = 5
#: The calendar a daily study's grid is built on.
GRID_SERIES = "msci_world_eur.level"

LEVELS = [
    "msci_world_eur.level",
    "msci_em_eur.level",
    "euro_cash.level",
    "bund_zero_05y.level",
    "bund_zero_17y.level",
    "tip.adj_close",
    "ief.adj_close",
    "gld.close",
    "crsox.adj_close",
    "ecb_fx.USD",
]
RATES = [
    "ecb_eonia.value",
    "ecb_estr.value",
    "bbk_bund_04y.value",
    "bbk_bund_05y.value",
    "bbk_bund_16y.value",
    "bbk_bund_17y.value",
]
RETURNS = [
    "kf_developed_daily.Mkt-RF",
    "kf_developed_daily.RF",
    "ff3_market.Mkt-RF",
    "ff3_market.RF",
    "aqr_century_market.Equity indices Market",
    "aqr_century_market.Fixed income Market",
]


def weekdays(first: date, last: date) -> list[date]:
    days, day = [], first
    while day <= last:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


def runs(flags: list[bool]) -> int:
    longest = current = 0
    for flag in flags:
        current = current + 1 if flag else 0
        longest = max(longest, current)
    return longest


def check(store, series_id: str, kind: str, grid: set[date]) -> dict:
    info = research_series_info(store, series_id)
    if info is None:
        return {"series": series_id, "loaded": False}
    rows = research_observations(store, series_id, as_of=date(2026, 10, 18))
    days = [row.observed_on for row in rows]
    values = [row.value for row in rows]
    out: dict = {
        "series": series_id,
        "vendor": info.vendor.value,
        "currency": info.currency,
        "units": info.units,
        "frequency": info.frequency.value,
        "point_in_time": info.point_in_time.value,
        "first": days[0].isoformat(),
        "last": days[-1].isoformat(),
        "rows": len(rows),
        "duplicate_days": sum(n - 1 for n in Counter(days).values() if n > 1),
        "not_finite": sum(not math.isfinite(v) for v in values),
        "lags_days": sorted({(row.available_on - row.observed_on).days for row in rows}),
    }
    if kind == "level":
        out["not_positive"] = sum(v <= 0 for v in values)
    if kind == "return":
        low, high = RETURN_BOUNDS
        out["outside_unit_bounds"] = [
            d.isoformat() for d, v in zip(days, values, strict=True) if not low < v < high
        ]
    if info.frequency.value == "daily":
        present = set(days)
        span = weekdays(days[0], days[-1])
        missing = [d not in present for d in span]
        by_year = Counter(d.year for d, gone in zip(span, missing, strict=True) if gone)
        out["weekdays_without_a_row"] = sum(missing)
        out["most_weekdays_without_a_row_in_a_year"] = max(by_year.values(), default=0)
        out["longest_run_of_weekdays_without_a_row"] = runs(missing)
        out["weekend_rows"] = sum(d.weekday() >= 5 for d in days)
        out["largest_calendar_gap_days"] = max((b - a).days for a, b in pairwise(days))
        on_grid = [d for d in sorted(grid) if days[0] <= d <= days[-1]]
        out["grid_days_without_a_row"] = sum(d not in present for d in on_grid)
        out["grid_days_in_span"] = len(on_grid)
        stale = [a == b for a, b in pairwise(values)]
        out[f"runs_of_{STALE_RUN}_or_more_unchanged"] = 0
        current = 0
        for flag in [*stale, False]:
            if flag:
                current += 1
            else:
                if current + 1 >= STALE_RUN:
                    out[f"runs_of_{STALE_RUN}_or_more_unchanged"] += 1
                current = 0
        out["longest_unchanged_run_rows"] = runs(stale) + 1
        if kind == "level":
            out["possible_splices"] = [
                b.isoformat()
                for (a, x), (b, y) in pairwise(zip(days, values, strict=True))
                if x > 0 and y > 0 and abs(math.log(y / x)) > SPLICE_LOG_CHANGE
            ]
    else:
        months = {(d.year, d.month) for d in days}
        first, last = days[0], days[-1]
        expected = (last.year - first.year) * 12 + last.month - first.month + 1
        out["months_without_a_row"] = expected - len(months)
        month_days = sorted({d.day for d in days})
        out["days_of_month"] = [month_days[0], month_days[-1]]
    return out


def main() -> int:
    env = Environment()
    loaded = load_config(env.resolved_config_dir())
    with open_research_store(loaded.settings, env.require_data_dir()) as store:
        grid = {
            row.observed_on
            for row in research_observations(store, GRID_SERIES, as_of=date(2026, 10, 18))
        }
        grid_weekend = sum(d.weekday() >= 5 for d in grid)
        results = {
            "grid": {"series": GRID_SERIES, "days": len(grid), "weekend_days": grid_weekend},
            "levels": [check(store, s, "level", grid) for s in LEVELS],
            "rates": [check(store, s, "rate", grid) for s in RATES],
            "returns": [check(store, s, "return", grid) for s in RETURNS],
        }
    json.dump(results, sys.stdout, indent=1)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
