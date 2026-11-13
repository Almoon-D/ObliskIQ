"""Span and filer coverage of the SEC's free filings the Hunter's hypotheses would read.

Exploration (DECISIONS D64): outside the coverage floor and never evidence of an effect. It
counts filings and filers by year; it reads no price and no financial value.

Input: EDGAR's quarterly full indexes (``master.gz``, every filing with its form type, filer CIK
and filing date; ``xbrl.gz``, the filings that carry XBRL), fetched with ``SEC_USER_AGENT`` as
the User-Agent (the SEC's fair-access rule).
Output (``edgar.json``): per year, the distinct filers of periodic reports (10-K, 10-Q and their
small-business forms), those whose periodic reports carry XBRL, the 8-K filers, and the original
registrations on Form 10 (10-12B, an exchange listing, the spin-off's form; 10-12G).

Usage: python -I research/explore/hunter_feasibility/edgar_index.py <out dir> <first> <last>
"""

import gzip
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path

INDEX = "https://www.sec.gov/Archives/edgar/full-index/{}/QTR{}/{}.gz"
PERIODIC = {"10-K", "10-Q", "10-K405", "10-KSB", "10-QSB", "10-KT", "10-QT", "10-KSB40"}
FORM10 = {"10-12B", "10-12G"}
#: The SEC asks for at most ten requests a second.
PAUSE = 0.2


def _get(url: str) -> bytes | None:
    agent = {"User-Agent": os.environ["SEC_USER_AGENT"]}
    for attempt in range(5):
        try:
            with urllib.request.urlopen(
                urllib.request.Request(url, headers=agent), timeout=120
            ) as r:
                return gzip.decompress(r.read())
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            time.sleep(2**attempt * 5)
        except urllib.error.URLError, TimeoutError:
            time.sleep(2**attempt * 5)
    raise RuntimeError(f"gave up on {url}")


def _rows(raw: bytes):
    for line in raw.decode("latin-1").splitlines():
        parts = line.split("|")
        if len(parts) == 5 and parts[0].isdigit():
            yield parts


def main(out: Path, first: int, last: int) -> None:
    out.mkdir(parents=True, exist_ok=True)
    path = out / "edgar.json"
    result: dict[str, dict[str, object]] = json.loads(path.read_text()) if path.exists() else {}
    for year in range(first, last + 1):
        if str(year) in result:
            continue
        periodic: set[str] = set()
        xbrl: set[str] = set()
        eight_k: set[str] = set()
        eight_k_count = 0
        form10: dict[str, set[str]] = defaultdict(set)
        quarters = 0
        for quarter in range(1, 5):
            master = _get(INDEX.format(year, quarter, "master"))
            time.sleep(PAUSE)
            if master is None:
                continue
            quarters += 1
            for cik, _name, form, _date, _file in _rows(master):
                if form in PERIODIC:
                    periodic.add(cik)
                elif form == "8-K":
                    eight_k.add(cik)
                    eight_k_count += 1
                elif form in FORM10:
                    form10[form].add(cik)
            tagged = _get(INDEX.format(year, quarter, "xbrl")) if year >= 2009 else None
            time.sleep(PAUSE)
            for cik, _name, form, _date, _file in _rows(tagged or b""):
                if form in PERIODIC:
                    xbrl.add(cik)
        result[str(year)] = {
            "quarters": quarters,
            "periodic_filers": len(periodic),
            "periodic_filers_with_xbrl": len(xbrl & periodic),
            "eight_k_filers": len(eight_k),
            "eight_k_filings": eight_k_count,
            "form10": {k: len(v) for k, v in sorted(form10.items())},
        }
        path.write_text(json.dumps(result, indent=1))
        print(year, result[str(year)], flush=True)


if __name__ == "__main__":
    main(Path(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3]))
