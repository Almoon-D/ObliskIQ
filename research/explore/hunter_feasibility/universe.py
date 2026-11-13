"""The US-listed common stocks on a recent date, with market capitalisation and daily traded value.

Exploration (DECISIONS D64): outside the coverage floor and never evidence of an effect. It
measures how many stocks pass the universe floors of ``research/reports/hunter-feasibility.md``;
it reads prices only for traded value (close times volume), never a return.

Input: Nasdaq's public stock screener (every stock listed on Nasdaq, the NYSE and NYSE American,
with its market capitalisation) and Yahoo's daily bars for the last three months.
Output (``universe.json``): per symbol its exchange, name, market capitalisation, last close and
the median of close times volume over its last ``SESSIONS`` sessions.

Usage: python -I research/explore/hunter_feasibility/universe.py <out dir>
"""

import json
import re
import statistics
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

EXCHANGES = ("nasdaq", "nyse", "amex")
SCREENER = "https://api.nasdaq.com/api/screener/stocks?tableonly=true&download=true&exchange={}"
CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{}?range=3mo&interval=1d"
AGENT = {"User-Agent": "Mozilla/5.0"}
#: Sessions the median daily traded value is taken over (the thesis book's own judgement,
#: config/defaults/thesis.yaml liquidity.sessions).
SESSIONS = 20
#: A common share: its name says so. Anything matching NOT is another security (a preferred,
#: a depositary share, a warrant, a unit, a right, a note, a closed-end fund).
COMMON = re.compile(
    r"common stock|common shares|ordinary share|class [a-c]\b|beneficial interest|capital stock",
    re.I,
)
NOT = re.compile(
    r"preferred|preference|depositary|warrant|\bunits?\b|\brights?\b|\bnotes?\b|\bdue 20|"
    r"\bfund\b|%|debenture|subordinated",
    re.I,
)
WORKERS = 6


def _get(url: str) -> bytes:
    for attempt in range(6):
        try:
            with urllib.request.urlopen(
                urllib.request.Request(url, headers=AGENT), timeout=30
            ) as r:
                return bytes(r.read())
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise
            time.sleep(2**attempt)
        except urllib.error.URLError, TimeoutError:
            time.sleep(2**attempt)
    raise RuntimeError(f"gave up on {url}")


def _traded_value(symbol: str) -> dict[str, object] | None:
    try:
        data = json.loads(_get(CHART.format(symbol.replace("/", "-").replace("^", "-P"))))
    except urllib.error.HTTPError, RuntimeError:
        return None
    result = (data.get("chart") or {}).get("result") or []
    if not result:
        return None
    quote = result[0]["indicators"]["quote"][0]
    pairs = [
        (c, v)
        for c, v in zip(quote.get("close") or [], quote.get("volume") or [], strict=False)
        if c is not None and v is not None
    ][-SESSIONS:]
    if len(pairs) < SESSIONS:
        return {"sessions": len(pairs)}
    return {
        "sessions": len(pairs),
        "close": pairs[-1][0],
        "median_value": statistics.median(c * v for c, v in pairs),
        "last_date": result[0]["timestamp"][-1],
    }


def main(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    rows: dict[str, dict[str, object]] = {}
    for exchange in EXCHANGES:
        raw = _get(SCREENER.format(exchange))
        (out / f"screener-{exchange}.json").write_bytes(raw)
        for row in json.loads(raw)["data"]["rows"]:
            name = row["name"]
            if not COMMON.search(name) or NOT.search(name):
                continue
            cap = float(row["marketCap"] or 0) or None
            rows[row["symbol"].strip()] = {"exchange": exchange, "name": name, "market_cap": cap}
    path = out / "universe.json"
    done: dict[str, dict[str, object]] = json.loads(path.read_text()) if path.exists() else {}
    todo = [s for s in rows if s not in done]
    print(f"{len(rows)} common stocks, {len(todo)} to fetch", flush=True)
    with ThreadPoolExecutor(WORKERS) as pool:
        for i, (symbol, value) in enumerate(zip(todo, pool.map(_traded_value, todo), strict=True)):
            done[symbol] = {**rows[symbol], "bars": value}
            if i % 250 == 0:
                path.write_text(json.dumps(done))
                print(i, flush=True)
    path.write_text(json.dumps(done))


if __name__ == "__main__":
    main(Path(sys.argv[1]))
