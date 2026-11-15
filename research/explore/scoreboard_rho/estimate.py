"""The average correlation between names in a forward-grading cohort, estimated from history
before the scoreboard's pre-registration fixes any minimum sample (DECISIONS D63).

Exploration (DECISIONS D64): outside the coverage floor and never evidence of an effect. It reads
no signal and no forecast: only the universe's tradable candidates' closes (`obliskiq research
data load-universe`) and the ECB's reference rates (`obliskiq research data load ecb_fx`), and
computes with the harness's own `panel_correlation` and `effective_n`.

For each listing with history, the closes are read on every Friday (the last close on or before
it), converted to the base currency (euro) at that day's rate, and turned into log returns over
non-overlapping four-week periods (every fourth Friday from the first). Each period's return less
the equity reference's (IWDA in euro) is a name's excess; the mean pairwise correlation of those
excess returns across names is rho. It is computed for the whole sample, for each half, for the
four phases of the four-week grid (so no start date is chosen) and for the subsets of the
universe a cohort could hold.

Usage: python -I research/explore/scoreboard_rho/estimate.py <repo> <data dir>
"""

import hashlib
import json
import math
import sys
from datetime import date, timedelta
from itertools import pairwise
from pathlib import Path

from obliskiq.config.loader import load_config
from obliskiq.research.data import universe_listings, universe_series_id
from obliskiq.research.stats import effective_n, panel_correlation
from obliskiq.store import open_research_store
from obliskiq.store.research import research_observations, research_series_info

REFERENCE = "IWDA"
FX_DATASET = "ecb_fx"
#: Four weeks: the scoreboard's default grading horizon, in weekly steps.
HORIZON_WEEKS = 4
#: A pair of names counts when both have this many common periods (two years of four-week
#: periods).
MIN_OVERLAP = 26
#: The sample: the reference's first full year to the last close loaded.
START = date(2010, 1, 1)
#: An FX rate older than this on a Friday leaves that Friday's price unconverted (a holiday week).
FX_MAX_AGE = timedelta(days=7)


def _fridays(start: date, end: date) -> list[date]:
    first = start + timedelta(days=(4 - start.weekday()) % 7)
    return [first + timedelta(weeks=k) for k in range((end - first).days // 7 + 1)]


def _on_or_before(points: list[tuple[date, float]], days: list[date]) -> dict[date, float]:
    found: dict[date, float] = {}
    index = 0
    latest: tuple[date, float] | None = None
    for day in days:
        while index < len(points) and points[index][0] <= day:
            latest = points[index]
            index += 1
        if latest is not None and day - latest[0] <= FX_MAX_AGE:
            found[day] = latest[1]
    return found


def main(repo: Path, data_dir: Path) -> None:
    settings = load_config(repo / "config").settings
    as_of = date.today()
    listings = universe_listings(settings)
    with open_research_store(settings, data_dir) as store:
        hashes: dict[str, str] = {}
        closes: dict[str, list[tuple[date, float]]] = {}
        currency: dict[str, str] = {}
        kind: dict[str, str] = {}
        classes = {i.symbol: i.asset_class.value for i in settings.universe.instruments}
        for listing in listings:
            series = universe_series_id(listing.symbol)
            info = research_series_info(store, series)
            if info is None:
                continue
            hashes[series] = info.content_sha256
            closes[listing.symbol] = [
                (o.observed_on, o.value) for o in research_observations(store, series, as_of=as_of)
            ]
            currency[listing.symbol] = listing.currency
            kind[listing.symbol] = classes.get(listing.symbol, "unknown")
        fx = {
            code: [
                (o.observed_on, o.value)
                for o in research_observations(store, f"{FX_DATASET}.{code}", as_of=as_of)
            ]
            for code in sorted(set(currency.values()) - {"EUR"})
        }
        for code in fx:
            info = research_series_info(store, f"{FX_DATASET}.{code}")
            if info is not None:
                hashes[f"{FX_DATASET}.{code}"] = info.content_sha256

    end = max(points[-1][0] for points in closes.values())
    fridays = _fridays(START, end)
    rates = {code: _on_or_before(points, fridays) for code, points in fx.items()}

    def in_euro(symbol: str) -> dict[date, float]:
        local = _on_or_before(closes[symbol], fridays)
        code = currency[symbol]
        if code == "EUR":
            return local
        return {day: value / rates[code][day] for day, value in local.items() if day in rates[code]}

    euro = {symbol: in_euro(symbol) for symbol in closes}

    def excess_panel(
        names: list[str], phase: int, first: date, last: date
    ) -> list[list[float | None]]:
        grid = [d for d in fridays[phase::HORIZON_WEEKS] if first <= d <= last]
        rows: list[list[float | None]] = []
        reference = euro[REFERENCE]
        for before, after in pairwise(grid):
            if before not in reference or after not in reference:
                continue
            ref = math.log(reference[after] / reference[before])
            row: list[float | None] = []
            for name in names:
                prices = euro[name]
                if before in prices and after in prices and prices[before] > 0:
                    row.append(math.log(prices[after] / prices[before]) - ref)
                else:
                    row.append(None)
            rows.append(row)
        return rows

    everyone = sorted(name for name in euro if name != REFERENCE)
    subsets = {
        "every tradable candidate": everyone,
        "equity funds and stocks": [
            n for n in everyone if kind[n] == "stock" or n in _equity_funds(settings)
        ],
        "stocks": [n for n in everyone if kind[n] == "stock"],
    }
    middle = START + (end - START) / 2
    report: dict[str, object] = {
        "as_of": as_of.isoformat(),
        "sample": [START.isoformat(), end.isoformat()],
        "horizon_weeks": HORIZON_WEEKS,
        "min_overlap_periods": MIN_OVERLAP,
        "reference": REFERENCE,
        "inputs_sha256": hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest(),
        "subsets": {},
    }
    for label, names in subsets.items():
        phases = [
            panel_correlation(excess_panel(names, p, START, end), min_overlap=MIN_OVERLAP)
            for p in range(HORIZON_WEEKS)
        ]
        halves = [
            panel_correlation(excess_panel(names, 0, START, middle), min_overlap=MIN_OVERLAP),
            panel_correlation(excess_panel(names, 0, middle, end), min_overlap=MIN_OVERLAP),
        ]
        pooled = [v for row in excess_panel(names, 0, START, end) for v in row if v is not None]
        mean = sum(pooled) / len(pooled)
        sd = math.sqrt(sum((v - mean) ** 2 for v in pooled) / (len(pooled) - 1))
        usable = [r for r in phases + halves if r is not None]
        rho = max(usable)
        report["subsets"][label] = {
            "names": len(names),
            "rho_by_phase": [None if r is None else round(r, 4) for r in phases],
            "rho_by_half": [None if r is None else round(r, 4) for r in halves],
            "rho_largest": round(rho, 4),
            "excess_sd_per_period": round(sd, 4),
            "n_eff_per_cohort": round(effective_n(len(names), rho), 2),
        }
    print(json.dumps(report, indent=2))


def _equity_funds(settings) -> set[str]:  # type: ignore[no-untyped-def]
    """Funds whose core role or grid cell is equity: a cohort of equity names."""
    found: set[str] = set()
    for declared in settings.universe.instruments:
        exposures = declared.exposures
        roles = set(exposures.core_roles)
        if declared.asset_class.value == "etf" and (
            exposures.grid is not None or roles & {"world_equity", "em_equity"}
        ):
            found.add(declared.symbol)
    return found


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
