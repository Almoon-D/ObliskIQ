"""How often a periodic report is preceded by an earnings release on Form 8-K (item 2.02).

Exploration (DECISIONS D64): outside the coverage floor and never evidence of an effect. It
measures whether post-earnings drift (H11) has a free, point-in-time announcement date for the
companies it would rank; it reads no price and no reported figure.

Input: EDGAR's quarterly ``master.gz`` indexes for the sampled years (the filers of 10-K and 10-Q)
and each sampled filer's submission history (``data.sec.gov/submissions``), with
``SEC_USER_AGENT`` as the User-Agent.
Output (``earnings.json``): per sampled year, the periodic reports of a seeded sample of filers
and the share preceded by an 8-K carrying item 2.02 (or item 12 before August 2004) within
``WINDOW`` days before the report's filing date, or ``AFTER`` days after it.

Usage: python -I research/explore/hunter_feasibility/earnings_dates.py <out dir>
"""

import gzip
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path

YEARS = (2006, 2011, 2016, 2021, 2025)
SAMPLE = 200
SEED = 20261113
INDEX = "https://www.sec.gov/Archives/edgar/full-index/{}/QTR{}/master.gz"
SUBMISSIONS = "https://data.sec.gov/submissions/{}"
PERIODIC = {"10-K", "10-Q", "10-K405", "10-KSB", "10-QSB"}
EARNINGS_ITEMS = ("2.02",)
WINDOW = 60
AFTER = 3
PAUSE = 0.15


def _get(url: str) -> bytes | None:
    agent = {"User-Agent": os.environ["SEC_USER_AGENT"]}
    for attempt in range(5):
        try:
            time.sleep(PAUSE)
            with urllib.request.urlopen(
                urllib.request.Request(url, headers=agent), timeout=60
            ) as r:
                return bytes(r.read())
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            time.sleep(2**attempt * 3)
        except urllib.error.URLError, TimeoutError:
            time.sleep(2**attempt * 3)
    raise RuntimeError(f"gave up on {url}")


def _filers(year: int) -> list[str]:
    ciks: set[str] = set()
    for quarter in range(1, 5):
        raw = _get(INDEX.format(year, quarter))
        if raw is None:
            continue
        for line in gzip.decompress(raw).decode("latin-1").splitlines():
            parts = line.split("|")
            if len(parts) == 5 and parts[0].isdigit() and parts[2] in PERIODIC:
                ciks.add(parts[0])
    return sorted(ciks)


def _filings(cik: str) -> list[tuple[str, date, str]]:
    raw = _get(SUBMISSIONS.format(f"CIK{int(cik):010d}.json"))
    if raw is None:
        return []
    data = json.loads(raw)
    pages = [data["filings"]["recent"]]
    for extra in data["filings"].get("files", []):
        more = _get(SUBMISSIONS.format(extra["name"]))
        if more is not None:
            pages.append(json.loads(more))
    rows = []
    for page in pages:
        items = page.get("items") or [""] * len(page["form"])
        for form, filed, item in zip(page["form"], page["filingDate"], items, strict=True):
            rows.append((form, date.fromisoformat(filed), item or ""))
    return rows


def _is_earnings(form: str, filed: date, items: str) -> bool:
    if form not in ("8-K", "8-K/A"):
        return False
    listed = {i.strip() for i in items.split(",")}
    return any(i in listed for i in EARNINGS_ITEMS) or (
        filed < date(2004, 8, 23) and "12" in listed
    )


def main(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    rng = random.Random(SEED)
    result = {}
    for year in YEARS:
        filers = _filers(year)
        sample = rng.sample(filers, min(SAMPLE, len(filers)))
        reports = with_release = filers_with_any = 0
        for cik in sample:
            rows = _filings(cik)
            releases = [f for form, f, items in rows if _is_earnings(form, f, items)]
            periodic = [f for form, f, _ in rows if form in PERIODIC and f.year == year]
            hit = 0
            for filed in periodic:
                low, high = filed - timedelta(days=WINDOW), filed + timedelta(days=AFTER)
                hit += any(low <= r <= high for r in releases)
            reports += len(periodic)
            with_release += hit
            filers_with_any += hit > 0
        result[str(year)] = {
            "filers": len(filers),
            "sampled": len(sample),
            "periodic_reports": reports,
            "preceded_by_2_02": with_release,
            "filers_with_any_2_02": filers_with_any,
        }
        print(year, result[str(year)], flush=True)
        (out / "earnings.json").write_text(json.dumps(result, indent=1))


if __name__ == "__main__":
    main(Path(sys.argv[1]))
