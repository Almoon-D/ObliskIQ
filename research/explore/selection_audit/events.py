"""Insider-purchase clusters from the parsed Form 4 purchases (exploration, DECISIONS D64).

A cluster event is the first filing date on which at least ``MIN_INSIDERS`` distinct officers or
directors of one issuer have reported open-market purchases whose trade dates lie within the
``WINDOW_DAYS`` before it (the third filing makes the cluster public). An issuer has no new event
within ``COOLDOWN_DAYS`` of its last one, so one issuer's events do not overlap at a six-month
horizon. These are the audit's counting rules; a pre-registration would fix its own.

Usage: python -I research/explore/selection_audit/events.py <out dir>
"""

import json
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

MIN_INSIDERS = 3
WINDOW_DAYS = 30
COOLDOWN_DAYS = 182


def clusters(purchases: list[dict]) -> list[dict]:
    rows = sorted(purchases, key=lambda r: (r["filed"], r["traded"]))
    events: list[dict] = []
    last: date | None = None
    for filed_text in sorted({r["filed"] for r in rows}):
        filed = date.fromisoformat(filed_text)
        if last is not None and filed - last < timedelta(days=COOLDOWN_DAYS):
            continue
        window = [
            r
            for r in rows
            if r["filed"] <= filed_text
            and filed - timedelta(days=WINDOW_DAYS) <= date.fromisoformat(r["traded"]) <= filed
        ]
        owners = {r["owner"] for r in window}
        if len(owners) < MIN_INSIDERS:
            continue
        value = sum(r["price"] * r["shares"] for r in window)
        shares = sum(r["shares"] for r in window)
        symbols = Counter(r["symbol"] for r in window if r["symbol"])
        events.append(
            {
                "filed": filed_text,
                "insiders": len(owners),
                "value": value,
                "vwap": value / shares,
                "first_trade": min(r["traded"] for r in window),
                "last_trade": max(r["traded"] for r in window),
                "symbol": symbols.most_common(1)[0][0] if symbols else None,
            }
        )
        last = filed
    return events


def main(out_dir: Path) -> None:
    data = json.loads((out_dir / "form4.json").read_text())
    events = []
    for cik, purchases in data["purchases"].items():
        for event in clusters(purchases):
            events.append({"cik": cik, "name": data["names"].get(cik, ""), **event})
    events.sort(key=lambda e: (e["filed"], e["cik"]))
    (out_dir / "events.json").write_text(json.dumps(events))
    by_year = Counter(e["filed"][:4] for e in events)
    print(len(events), "events;", len({e["cik"] for e in events}), "issuers")
    for year in sorted(by_year):
        print(year, by_year[year])


if __name__ == "__main__":
    main(Path(sys.argv[1]))
