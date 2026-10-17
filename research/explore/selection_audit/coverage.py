"""Measure the survivorship of free stock prices for H2 and H4 (exploration, DECISIONS D64).

For each insider-purchase cluster (H2) and each sampled issuer-year with a priced open-market
trade (H4's stock universe), is there a Yahoo history today that is the same security? A
history is the same security when its unadjusted close (Yahoo's split-adjusted close times
every split after the date) around the trade dates is within ``PRICE_TOLERANCE`` (in log terms)
of the price the insiders reported. A reported symbol that Yahoo now uses for another company
fails that check, so a reused ticker does not count as covered. The SEC's current ticker of the
issuer is tried beside the reported one, so a company that changed its ticker is not counted as
lost.

Outcomes per row: ``matched``; ``split_factor`` (the price is off by a whole split factor:
counted as covered, since only the adjustment is wrong); ``mismatch`` (a history exists at the
date but is another security or wrong); ``no_history_at_date``; ``no_history``; ``unknown`` (a
candidate symbol kept failing to download or was not asked for, so nothing is known: left out of
the shares). Lost =
no_history, no_history_at_date and mismatch, as a share of the rows that are not unknown.
Shares by year come with a Wilson 95 % interval.

Where Tiingo's public list of tickers (``supported_tickers.csv``, no key needed) is in the output
directory, each lost event is also looked up there: a US stock ticker (reported or current)
whose listed span covers the event's filing date. That measures how much a source that keeps
delisted tickers could recover; the list's tickers are not checked against the reported price.

Also measured, for power only and never on an event window: from the sampled stocks that are
matched, the standard deviation of six-month (``HORIZON_SESSIONS``) log returns in excess of
SPY over the year after the sampled trade, and the mean pairwise correlation of those excess
returns among stocks sampled in the same half-year (the rho of n_eff).

Usage: python research/explore/selection_audit/coverage.py <out dir> (it imports yahoo.py beside it)
"""

import csv
import json
import math
import sys
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path
from statistics import NormalDist

import numpy as np
from yahoo import yahoo_symbol

#: Reported prices within this log distance of the close are the same security (+/- ~22 %).
PRICE_TOLERANCE = 0.2
#: Ratios of whole splits that Yahoo sometimes misses or applies twice.
SPLIT_FACTORS = (2, 3, 4, 5, 8, 10, 15, 20, 25, 30, 40, 50, 100)
SPLIT_TOLERANCE = 0.08
#: Sessions searched on each side of the trade dates.
NEAR_DAYS = 4
#: Six months of sessions.
HORIZON_SESSIONS = 126
MARKET = "SPY"
Z95 = NormalDist().inv_cdf(0.975)


class History:
    def __init__(self, rows: list) -> None:
        self.days = [date.fromisoformat(r[0]) for r in rows]
        close = np.array([r[1] for r in rows], dtype=float)
        splits = np.array([r[2] if r[2] > 0 else 1.0 for r in rows], dtype=float)
        # The factor that undoes every split after each day.
        after = np.concatenate([np.cumprod(splits[::-1])[::-1][1:], [1.0]])
        self.close = close
        self.raw = close * after

    def window(self, first: date, last: date) -> slice:
        return slice(
            bisect_left(self.days, first - timedelta(days=NEAR_DAYS)),
            bisect_right(self.days, last + timedelta(days=NEAR_DAYS)),
        )


def load(out_dir: Path) -> dict[str, History]:
    histories: dict[str, History] = {}
    for path in sorted((out_dir / "yahoo").glob("*.json")):
        for symbol, rows in json.loads(path.read_text()).items():
            if rows:
                histories[symbol] = History(rows)
    return histories


def check(
    histories: dict[str, History],
    absent: set[str],
    symbols: list[str],
    first: date,
    last: date,
    price: float,
) -> tuple[str, str | None]:
    best = "no_history"
    for symbol in symbols:
        history = histories.get(symbol)
        if history is None:
            if symbol not in absent and best == "no_history":
                best = "unknown"
            continue
        span = history.window(first, last)
        if span.start >= span.stop:
            best = "no_history_at_date" if best == "no_history" else best
            continue
        gap = math.log(price / float(np.median(history.raw[span])))
        if abs(gap) <= PRICE_TOLERANCE:
            return "matched", symbol
        if any(abs(abs(gap) - math.log(f)) <= SPLIT_TOLERANCE for f in SPLIT_FACTORS):
            best = "split_factor"
            found = symbol
            continue
        if best != "split_factor":
            best = "mismatch"
    return best, (found if best == "split_factor" else None)


def wilson(k: int, n: int) -> tuple[float, float]:
    if n == 0:
        return math.nan, math.nan
    p = k / n
    centre = (p + Z95**2 / (2 * n)) / (1 + Z95**2 / n)
    half = Z95 * math.sqrt(p * (1 - p) / n + Z95**2 / (4 * n * n)) / (1 + Z95**2 / n)
    return centre - half, centre + half


def tiingo_spans(out_dir: Path) -> dict[str, list[tuple[str, str]]] | None:
    """US stock tickers in Tiingo's public list and their (start, end) spans."""
    path = out_dir / "supported_tickers.csv"
    if not path.exists():
        return None
    spans: dict[str, list[tuple[str, str]]] = defaultdict(list)
    with path.open() as handle:
        for row in csv.DictReader(handle):
            if row["assetType"] == "Stock" and row["priceCurrency"] == "USD" and row["startDate"]:
                spans[row["ticker"].upper()].append((row["startDate"], row["endDate"] or "9999"))
    return spans


def summarise(rows: list[dict], label: str) -> dict:
    by_year: dict[str, Counter] = defaultdict(Counter)
    for row in rows:
        by_year[row["year"]][row["outcome"]] += 1
    table = {}
    for year in sorted(by_year):
        counts = by_year[year]
        n = sum(counts.values()) - counts["unknown"]
        if n == 0:
            continue
        covered = counts["matched"] + counts["split_factor"]
        lost = n - covered
        low, high = wilson(lost, n)
        table[year] = {"n": n, "lost": lost, "share": lost / n, "low": low, "high": high, **counts}
    total = Counter(row["outcome"] for row in rows)
    print(label, dict(total))
    for year, entry in table.items():
        print(
            f"  {year}  n={entry['n']:5d}  lost {entry['share']:6.1%} "
            f"[{entry['low']:.1%}, {entry['high']:.1%}]  "
            f"matched {entry.get('matched', 0)}  split {entry.get('split_factor', 0)}  "
            f"mismatch {entry.get('mismatch', 0)}  none-at-date "
            f"{entry.get('no_history_at_date', 0)}  none {entry.get('no_history', 0)}  "
            f"unknown {entry.get('unknown', 0)}"
        )
    return table


def dispersion(histories: dict[str, History], rows: list[dict]) -> dict:
    """The sd of six-month log returns in excess of the market, and their mean pairwise
    correlation among stocks of the same half-year, over the year after each sampled trade."""
    market = histories[MARKET]
    index = {d: i for i, d in enumerate(market.days)}
    by_half: dict[str, list[tuple[str, np.ndarray]]] = defaultdict(list)
    excess_all: list[float] = []
    for row in rows:
        if row["outcome"] != "matched":
            continue
        history = histories[row["symbol_used"]]
        start = bisect_left(history.days, date.fromisoformat(row["traded"]))
        days = history.days[start : start + 2 * HORIZON_SESSIONS + 1]
        if len(days) < 2 * HORIZON_SESSIONS + 1 or any(d not in index for d in days):
            continue
        stock = np.log(history.close[start : start + 2 * HORIZON_SESSIONS + 1])
        bench = np.log(np.array([market.close[index[d]] for d in days]))
        steps = (stock - bench)[::HORIZON_SESSIONS]
        moves = np.diff(steps)
        if not np.all(np.isfinite(moves)):
            continue
        excess_all.extend(moves.tolist())
        traded = date.fromisoformat(row["traded"])
        by_half[f"{traded.year}H{1 + (traded.month > 6)}"].append((row["cik"], moves))
    pairs = []
    for members in by_half.values():
        values = np.array([m for _, m in members])
        if len(values) < 3:
            continue
        # Both six-month moves of every stock of the half-year, demeaned per move.
        centred = values - values.mean(axis=0)
        cov = centred @ centred.T / centred.shape[1]
        sd = np.sqrt(np.diag(cov))
        ok = sd > 0
        corr = cov[np.ix_(ok, ok)] / np.outer(sd[ok], sd[ok])
        upper = corr[np.triu_indices_from(corr, k=1)]
        pairs.extend(upper.tolist())
    excess = np.array(excess_all)
    return {
        "observations": int(excess.size),
        "sd_six_month_excess": float(excess.std(ddof=1)),
        "mean_pairwise_correlation": float(np.mean(pairs)) if pairs else None,
        "pairs": len(pairs),
    }


def main(out_dir: Path) -> None:
    histories = load(out_dir)
    status = json.loads((out_dir / "yahoo-status.json").read_text())
    # A symbol counts as absent only when Yahoo said so; one that failed or was never asked
    # for leaves its row unknown.
    absent = {symbol for symbol, state in status.items() if state == "absent"}
    events = json.loads((out_dir / "events.json").read_text())
    sample = json.loads((out_dir / "sample.json").read_text())
    current = json.loads((out_dir / "company_tickers_exchange.json").read_text())
    tickers: dict[str, list[str]] = defaultdict(list)
    for cik, _name, ticker, _exchange in current["data"]:
        tickers[str(cik)].append(ticker)

    def candidates(row: dict) -> list[str]:
        found = [yahoo_symbol(s) for s in [row["symbol"], *tickers.get(row["cik"], [])] if s]
        return list(dict.fromkeys(s for s in found if s))

    tiingo = tiingo_spans(out_dir)
    recoverable: Counter = Counter()
    event_rows = []
    horizon_short = Counter()
    for event in events:
        outcome, used = check(
            histories,
            absent,
            candidates(event),
            date.fromisoformat(event["first_trade"]),
            date.fromisoformat(event["last_trade"]),
            event["vwap"],
        )
        if outcome == "matched" and used is not None:
            history = histories[used]
            after = bisect_left(history.days, date.fromisoformat(event["filed"]))
            if after + HORIZON_SESSIONS >= len(history.days):
                horizon_short[event["filed"][:4]] += 1
        event_rows.append({"year": event["filed"][:4], "outcome": outcome})
        if tiingo is not None and outcome in {"no_history", "no_history_at_date", "mismatch"}:
            symbols = [s.replace("-", ".") for s in candidates(event)]
            if any(
                start <= event["filed"] <= end
                for symbol in symbols
                for start, end in tiingo.get(symbol, [])
            ):
                recoverable[event["filed"][:4]] += 1
    h2 = summarise(event_rows, "H2 events")
    print(
        "  matched events whose history ends inside six months:",
        dict(sorted(horizon_short.items())),
    )
    if tiingo is not None:
        print(
            "  lost events Tiingo's list names over the filing date:",
            dict(sorted(recoverable.items())),
        )

    sample_rows = []
    for row in sample:
        if not 2006 <= row["year"] <= 2026:
            continue  # a trade date typed outside the data sets' span
        traded = date.fromisoformat(row["traded"])
        outcome, used = check(histories, absent, candidates(row), traded, traded, row["price"])
        sample_rows.append(
            {**row, "year": str(row["year"]), "outcome": outcome, "symbol_used": used}
        )
    h4 = summarise(sample_rows, "H4 sample")
    spread = dispersion(histories, sample_rows)
    print("dispersion", spread)
    issuers = Counter(e["filed"][:4] for e in events)
    (out_dir / "coverage.json").write_text(
        json.dumps(
            {
                "h2": h2,
                "h2_horizon_short": horizon_short,
                "h2_tiingo_listed": recoverable,
                "h4": h4,
                "dispersion": spread,
                "events_by_year": issuers,
                "symbols_with_history": len(histories),
            }
        )
    )


if __name__ == "__main__":
    main(Path(sys.argv[1]))
