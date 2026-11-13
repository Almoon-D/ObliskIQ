"""The share of today's listed US common stocks whose periodic reports carry XBRL.

Exploration (DECISIONS D64): outside the coverage floor and never evidence of an effect. It
joins ``universe.json`` (``universe.py``) to the SEC's ticker list and counts, by size tier, the
stocks that filed a periodic report (10-K, 10-Q, 20-F, 40-F) with XBRL in the last four
quarters of EDGAR's full index, which is what the Financial Statement Data Sets are built from.

Usage: python -I research/explore/hunter_feasibility/xbrl_listed.py <universe.json> <out dir>
"""

import gzip
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

TICKERS = "https://www.sec.gov/files/company_tickers_exchange.json"
INDEX = "https://www.sec.gov/Archives/edgar/full-index/{}/QTR{}/xbrl.gz"
QUARTERS = ((2025, 4), (2026, 1), (2026, 2), (2026, 3))
FORMS = {"10-K", "10-Q", "20-F", "40-F", "10-K/A", "10-Q/A", "20-F/A", "40-F/A"}
TIERS = (("micro", 0, 3e8), ("small", 3e8, 2e9), ("mid", 2e9, 1e10), ("large", 1e10, float("inf")))


def _get(url: str) -> bytes:
    time.sleep(0.2)
    request = urllib.request.Request(url, headers={"User-Agent": os.environ["SEC_USER_AGENT"]})
    with urllib.request.urlopen(request, timeout=120) as r:
        return bytes(r.read())


def main(path: Path, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    table = json.loads(_get(TICKERS))
    cik_of = {row[2]: str(row[0]) for row in table["data"]}
    tagged: set[str] = set()
    for year, quarter in QUARTERS:
        for line in (
            gzip.decompress(_get(INDEX.format(year, quarter))).decode("latin-1").splitlines()
        ):
            parts = line.split("|")
            if len(parts) == 5 and parts[0].isdigit() and parts[2] in FORMS:
                tagged.add(parts[0])
    stocks = json.loads(path.read_text())
    result = {}
    for name, low, high in TIERS:
        members = [
            s for s, v in stocks.items() if v["market_cap"] and low <= v["market_cap"] < high
        ]
        mapped = [s for s in members if s.replace("/", "-") in cik_of or s in cik_of]
        with_xbrl = [s for s in mapped if cik_of.get(s, cik_of.get(s.replace("/", "-"))) in tagged]
        result[name] = {
            "stocks": len(members),
            "with_cik": len(mapped),
            "with_xbrl": len(with_xbrl),
        }
    (out / "xbrl_listed.json").write_text(json.dumps(result, indent=1))
    print(json.dumps(result, indent=1))


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
