"""Download Yahoo daily histories for the audit's symbols and keep them (exploration, D64).

The symbols are the ones the audit checks: each cluster event's reported symbol, each sampled
issuer-year's reported symbol (the stock-level universe, ``SAMPLE_PER_YEAR`` a year, seeded),
and the SEC's current ticker of every such issuer (``company_tickers_exchange.json``), so a
company that changed its ticker is not counted as lost. Histories are kept as JSON in the cache
(date, close, split ratio on that date), one file per chunk, so the evaluation can be re-run
without the network. Each symbol is asked for on its own with yfinance's exceptions on, so a
symbol Yahoo does not know (``absent``) is told apart from a request it throttled (retried
after a growing wait) or one that kept failing (``failed``, reported, never counted as lost).
``sample.json`` records the sampled issuer-years.

Usage: python -I research/explore/selection_audit/yahoo.py <out dir>
"""

import json
import math
import random
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import yfinance as yf
from yfinance.exceptions import YFInvalidPeriodError, YFRateLimitError, YFTickerMissingError

SAMPLE_PER_YEAR = 200
SEED = 20261017
WORKERS = 4
CHUNK = 100
#: A throttled request waits and doubles its wait; another failure is retried briefly.
FIRST_WAIT = 20.0
ATTEMPTS = 6
OTHER_WAIT = 3.0
OTHER_ATTEMPTS = 3
BENCHMARKS = ("SPY", "IWM")


def yahoo_symbol(symbol: str) -> str | None:
    """Yahoo's spelling of a US symbol: a class suffix after a dash, nothing else exotic."""
    cleaned = symbol.strip().upper().lstrip("$").replace("/", "-").replace(".", "-")
    if not cleaned or not all(c.isalnum() or c == "-" for c in cleaned) or len(cleaned) > 10:
        return None
    return cleaned


def main(out_dir: Path) -> None:
    form4 = json.loads((out_dir / "form4.json").read_text())
    events = json.loads((out_dir / "events.json").read_text())
    current = json.loads((out_dir / "company_tickers_exchange.json").read_text())
    tickers: dict[str, list[str]] = defaultdict(list)
    for cik, _name, ticker, _exchange in current["data"]:
        tickers[str(cik)].append(ticker)
    by_year: dict[int, list[dict]] = defaultdict(list)
    for row in form4["priced"]:
        by_year[row["year"]].append(row)
    rng = random.Random(SEED)
    sample = []
    for year in sorted(by_year):
        rows = sorted(by_year[year], key=lambda r: r["cik"])
        sample += rng.sample(rows, min(SAMPLE_PER_YEAR, len(rows)))
    (out_dir / "sample.json").write_text(json.dumps(sample))
    # The market, for the dispersion of excess returns; the small-cap index beside it.
    wanted: set[str] = set(BENCHMARKS)
    for row in [*events, *sample]:
        for symbol in [row["symbol"], *tickers.get(row["cik"], [])]:
            converted = yahoo_symbol(symbol) if symbol else None
            if converted:
                wanted.add(converted)
    cache = out_dir / "yahoo"
    cache.mkdir(exist_ok=True)
    known: dict[str, str] = {}
    for path in cache.glob("*.json"):
        for symbol, rows in json.loads(path.read_text()).items():
            if rows:
                known[symbol] = "history"
    status_path = out_dir / "yahoo-status.json"
    if status_path.exists():
        known |= {k: v for k, v in json.loads(status_path.read_text()).items() if v == "absent"}
    todo = sorted(wanted - set(known))
    print(len(wanted), "symbols;", len(todo), "to fetch", flush=True)
    number = len(list(cache.glob("*.json")))
    # Processes, not threads: yfinance keeps shared state that threads stalled on.
    with ProcessPoolExecutor(max_workers=WORKERS) as pool:
        for first in range(0, len(todo), CHUNK):
            chunk = todo[first : first + CHUNK]
            results = list(pool.map(fetch, chunk))
            kept = {
                symbol: rows for symbol, (status, rows) in zip(chunk, results, strict=True) if rows
            }
            (cache / f"single-{number:04d}.json").write_text(json.dumps(kept))
            number += 1
            for symbol, (status, _rows) in zip(chunk, results, strict=True):
                known[symbol] = status
            status_path.write_text(json.dumps(known))
            counts = Counter(status for status, _ in results)
            print(first + len(chunk), "of", len(todo), dict(counts), flush=True)


def fetch(symbol: str) -> tuple[str, list]:
    """``history`` with its rows, ``absent`` when Yahoo has no such symbol, ``failed`` after
    the retries; a rate limit waits and retries."""
    yf.config.debug.hide_exceptions = False
    wait = FIRST_WAIT
    others = 0
    for _attempt in range(ATTEMPTS):
        try:
            frame = yf.Ticker(symbol).history(period="max", auto_adjust=False, actions=True)
        except YFRateLimitError:
            time.sleep(wait)
            wait *= 2
            continue
        except YFTickerMissingError, YFInvalidPeriodError:
            # Unknown to Yahoo, or a quote without a daily history.
            return "absent", []
        except Exception as exc:
            if "404" in str(exc):
                # Yahoo's "Quote not found": the symbol is unknown to it.
                return "absent", []
            print(symbol, type(exc).__name__, str(exc)[:120], file=sys.stderr, flush=True)
            others += 1
            if others >= OTHER_ATTEMPTS:
                break
            time.sleep(OTHER_WAIT)
            continue
        if frame.empty or "Close" not in frame.columns:
            return "absent", []
        splits = frame["Stock Splits"] if "Stock Splits" in frame.columns else [0.0] * len(frame)
        rows = []
        for stamp, close, split in zip(frame.index, frame["Close"], splits, strict=True):
            if close is None or (isinstance(close, float) and math.isnan(close)):
                continue
            ratio = 0.0 if split is None or math.isnan(split) else float(split)
            rows.append([stamp.date().isoformat(), float(close), ratio])
        return ("history", rows) if rows else ("absent", [])
    return "failed", []


if __name__ == "__main__":
    main(Path(sys.argv[1]))
