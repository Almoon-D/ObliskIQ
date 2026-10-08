#!/usr/bin/env python3
"""
Backfill CFTC COT History (>= 3 years) — v6.10.0
================================================

One-time ingest of the CFTC legacy futures-only ANNUAL archives into the
warehouse ``cot_history`` table, so the Asymmetry Hunter's crowding percentile
is MEANINGFUL on day one (unlike IV, COT history IS downloadable — use the gift).

Source (per year, 1986–present):
    https://www.cftc.gov/files/dea/history/deacot{YEAR}.zip   →  annual.txt (CSV)

Each ZIP holds one weekly-row CSV for ALL markets; we filter to the tracked
contracts (``CFTCCOTAnalyzer.TRACKED_CONTRACTS``) and UPSERT every weekly row via
``DataWarehouse.store_cot_snapshot`` (idempotent per contract_code/report_date,
so re-running is safe). Parsing is delegated to the SHARED
``parse_cot_annual_zip`` used by the live Phase-1 fetch (DRY).

Default range is 2015→present: ≥3 years for live percentiles AND enough prior
history for the replay's 2018Q4 window.

Usage:
    python scripts/backfill_cot_history.py                       # 2015->present
    python scripts/backfill_cot_history.py --start-year 2010
    python scripts/backfill_cot_history.py --contracts CL,GC,SP500
    python scripts/backfill_cot_history.py --verify              # report only

Downloads are reassembled via HTTP Range requests so they survive proxy/body
size caps (the CFTC/Cloudflare origin advertises ``Accept-Ranges: bytes``).
"""

from __future__ import annotations

import argparse
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import List, Optional

# Make both import styles work (the package mixes `modules` and `ObliskIQ.modules`).
_PKG_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PKG_ROOT))
sys.path.insert(0, str(_PKG_ROOT.parent))

from modules.market.cftc_cot_analyzer import (  # noqa: E402
    CFTCCOTAnalyzer,
    parse_cot_annual_zip,
)
from modules.data.warehouse import DataWarehouse  # noqa: E402

_UA = {"User-Agent": "Mozilla/5.0 (ObliskIQ COT backfill)"}
_CHUNK = 512 * 1024


def _download_full(url: str, retries: int = 4) -> Optional[bytes]:
    """
    Download ``url`` fully, reassembling via HTTP Range so the transfer survives
    proxy / body-size caps. Returns None for a missing year (HTTP 404) or after
    exhausting retries (exponential backoff).
    """
    for attempt in range(retries):
        try:
            head = urllib.request.Request(url, headers=_UA, method="HEAD")
            total = int(urllib.request.urlopen(head, timeout=60).headers["Content-Length"])
            buf = bytearray()
            start = 0
            while start < total:
                end = min(start + _CHUNK - 1, total - 1)
                req = urllib.request.Request(
                    url, headers={**_UA, "Range": f"bytes={start}-{end}"}
                )
                part = urllib.request.urlopen(req, timeout=120).read()
                if not part:
                    break
                buf.extend(part)
                start += len(part)
            if len(buf) == total and total > 0:
                return bytes(buf)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None  # year not published
        except Exception:
            pass
        time.sleep(2 ** attempt)
    return None


class COTBackfiller:
    """Backfill historical CFTC COT data into the warehouse."""

    def __init__(self, warehouse: DataWarehouse, contracts: Optional[List[str]] = None):
        self.w = warehouse
        tracked = CFTCCOTAnalyzer.TRACKED_CONTRACTS
        if contracts:
            keys = {c.strip().upper() for c in contracts}
            self.tracked = {k: v for k, v in tracked.items() if k.upper() in keys}
            if not self.tracked:
                raise SystemExit(f"No tracked contracts match {sorted(keys)}")
        else:
            self.tracked = dict(tracked)

    def backfill(self, start_year: int, end_year: int) -> int:
        print(f"\n{'=' * 70}")
        print(f"CFTC COT HISTORY BACKFILL — {start_year}..{end_year}")
        print(f"{'=' * 70}")
        print(f"Contracts: {', '.join(sorted(self.tracked))}\n")

        total = 0
        for year in range(start_year, end_year + 1):
            url = CFTCCOTAnalyzer.COT_HISTORY_URL_TEMPLATE.format(year=year)
            print(f"[{year}] {url}")
            raw = _download_full(url)
            if not raw:
                print("   ⚠️  skipped (no file / download failed)")
                continue
            try:
                df = parse_cot_annual_zip(raw, self.tracked)
            except Exception as exc:
                print(f"   ❌ parse error: {exc}")
                continue
            if df.empty:
                print("   ⚠️  no tracked-contract rows")
                continue

            rows = []
            for _, r in df.iterrows():
                ls = (r["large_spec_long"] or 0) - (r["large_spec_short"] or 0)
                cm = (r["commercial_long"] or 0) - (r["commercial_short"] or 0)
                sm = (r["small_spec_long"] or 0) - (r["small_spec_short"] or 0)
                rd = r["report_date"]
                rows.append({
                    "contract_code": str(r["contract_code"]),
                    "contract_name": r["contract_name"],
                    "report_date": rd.isoformat() if hasattr(rd, "isoformat") else str(rd),
                    "large_spec_net": int(ls),
                    "commercial_net": int(cm),
                    "small_spec_net": int(sm),
                    "open_interest": int(r["open_interest"]),
                })
            n = self.w.store_cot_snapshot(rows)
            total += n
            print(f"   ✅ stored {n} weekly rows ({df['contract_name'].nunique()} contracts)")
            time.sleep(0.5)  # be polite to the CFTC origin

        print(f"\nTotal rows stored: {total:,}")
        return total

    def verify(self) -> bool:
        print(f"\n{'=' * 70}")
        print("VERIFICATION")
        print(f"{'=' * 70}")
        ok = True
        cursor = self.w.conn.cursor()
        for key, meta in sorted(self.tracked.items()):
            code = meta["cftc_code"]
            row = cursor.execute(
                "SELECT COUNT(*) AS n, MIN(report_date) AS lo, MAX(report_date) AS hi "
                "FROM cot_history WHERE contract_code = ?",
                (code,),
            ).fetchone()
            n = int(row["n"] or 0)
            span = f"{row['lo']} → {row['hi']}" if n else "—"
            warm = "WARM" if n >= self.w.MIN_COT_HISTORY_WEEKS else "WARMING-UP"
            flag = "✅" if n >= self.w.MIN_COT_HISTORY_WEEKS else "⚠️"
            print(f"  {flag} {meta['name']:<20} ({code:<7}) {n:>4} wk  [{warm:<10}]  {span}")
            if n < self.w.MIN_COT_HISTORY_WEEKS:
                ok = False
        print(f"\nTotal COT rows: {self.w.count_cot_history():,}  ·  latest: {self.w.latest_cot_date()}")
        print("✅ All tracked contracts warm" if ok else "⚠️  Some contracts still warming up (<52 wk)")
        return ok


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill CFTC COT history into the warehouse")
    parser.add_argument("--start-year", type=int, default=2015, help="First year to fetch (default 2015)")
    parser.add_argument("--end-year", type=int, default=datetime.utcnow().year, help="Last year (default current)")
    parser.add_argument("--contracts", type=str, help="Tracked contract keys, comma-separated (default all)")
    parser.add_argument("--db", type=str, default=None, help="Warehouse DB path (default: bundled)")
    parser.add_argument("--verify", action="store_true", help="Report coverage only (no fetching)")
    args = parser.parse_args()

    warehouse = DataWarehouse(db_path=args.db)
    contracts = [c for c in args.contracts.split(",")] if args.contracts else None
    backfiller = COTBackfiller(warehouse, contracts=contracts)

    if args.verify:
        return 0 if backfiller.verify() else 1

    backfiller.backfill(args.start_year, args.end_year)
    backfiller.verify()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
