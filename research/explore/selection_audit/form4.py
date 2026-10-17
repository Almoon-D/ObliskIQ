"""Parse the SEC insider-transaction data sets (Forms 3, 4 and 5) into what the data audit needs.

Exploration (DECISIONS D64): outside the coverage floor and never evidence of an effect. It
measures the data a selection hypothesis would stand on; see
``research/reports/selection-data-audit.md``.

Input: the quarterly ``<yyyy>q<n>_form345.zip`` files of the SEC's "Insider Transactions Data
Sets" page, downloaded as they are. Output (``form4.json``): per issuer CIK, its open-market
purchases by officers and directors on original Forms 4, and one priced open-market trade
(purchase or sale) per issuer and year, for the identity check against Yahoo.

Usage: python -I research/explore/selection_audit/form4.py <zip dir> <out dir>
"""

import csv
import hashlib
import io
import json
import sys
import zipfile
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

csv.field_size_limit(sys.maxsize)

#: An open-market or private purchase, and an open-market or private sale (SEC Form 4 codes).
PURCHASE = "P"
SALE = "S"
#: Officers and directors; a holder of ten per cent who is neither is left out.
INSIDER_ROLES = ("Director", "Officer")
#: Symbols the filings use for "no listed class".
NO_SYMBOL = {"", "NONE", "N/A", "NA", "NOT APPLICABLE", "NO SYMBOL", "N.A.", "-", "--"}


def _rows(archive: zipfile.ZipFile, name: str):
    with archive.open(name) as handle:
        text = io.TextIOWrapper(handle, "utf-8", errors="replace")
        yield from csv.DictReader(text, delimiter="\t", quoting=csv.QUOTE_NONE)


def _day(text: str) -> date | None:
    try:
        return datetime.strptime(text.strip(), "%d-%b-%Y").date()
    except ValueError:
        return None


def _number(text: str) -> float | None:
    try:
        value = float(text)
    except ValueError:
        return None
    return value if value > 0 else None


def _symbol(text: str) -> str | None:
    cleaned = text.strip().upper()
    if cleaned in NO_SYMBOL or " " in cleaned or "," in cleaned or ";" in cleaned:
        return None
    return cleaned


def parse(zip_dir: Path, out_dir: Path) -> None:
    purchases: dict[str, list[dict]] = defaultdict(list)
    priced: dict[tuple[str, int], dict] = {}
    names: dict[str, str] = {}
    hashes: dict[str, str] = {}
    totals: dict[str, int] = defaultdict(int)
    for path in sorted(zip_dir.glob("*_form345.zip")):
        hashes[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
        archive = zipfile.ZipFile(path)
        filings: dict[str, tuple[date, str, str | None]] = {}
        for row in _rows(archive, "SUBMISSION.tsv"):
            if row["DOCUMENT_TYPE"] != "4":
                continue
            filed = _day(row["FILING_DATE"])
            if filed is None:
                continue
            cik = row["ISSUERCIK"].strip().lstrip("0")
            filings[row["ACCESSION_NUMBER"]] = (filed, cik, _symbol(row["ISSUERTRADINGSYMBOL"]))
            names[cik] = row["ISSUERNAME"].strip()
        owners: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for row in _rows(archive, "REPORTINGOWNER.tsv"):
            if row["ACCESSION_NUMBER"] in filings:
                owners[row["ACCESSION_NUMBER"]].append(
                    (row["RPTOWNERCIK"].strip().lstrip("0"), row["RPTOWNER_RELATIONSHIP"])
                )
        for row in _rows(archive, "NONDERIV_TRANS.tsv"):
            accession = row["ACCESSION_NUMBER"]
            if accession not in filings:
                continue
            code, direction = row["TRANS_CODE"].strip(), row["TRANS_ACQUIRED_DISP_CD"].strip()
            if (code, direction) not in {(PURCHASE, "A"), (SALE, "D")}:
                continue
            traded = _day(row["TRANS_DATE"])
            price = _number(row["TRANS_PRICEPERSHARE"])
            shares = _number(row["TRANS_SHARES"])
            filed, cik, symbol = filings[accession]
            if traded is None or price is None or shares is None or traded > filed:
                continue
            totals[f"{code} rows"] += 1
            key = (cik, traded.year)
            if symbol is not None and key not in priced:
                priced[key] = {"symbol": symbol, "traded": traded.isoformat(), "price": price}
            if code != PURCHASE:
                continue
            insiders = [
                owner
                for owner, roles in owners[accession]
                if any(r in roles for r in INSIDER_ROLES)
            ]
            for owner in insiders:
                purchases[cik].append(
                    {
                        "filed": filed.isoformat(),
                        "traded": traded.isoformat(),
                        "owner": owner,
                        "price": price,
                        "shares": shares,
                        "symbol": symbol,
                    }
                )
        print(path.name, len(filings), sum(len(v) for v in purchases.values()), flush=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "form4.json").write_text(
        json.dumps(
            {
                "sources": hashes,
                "names": names,
                "purchases": purchases,
                "priced": [{"cik": c, "year": y, **v} for (c, y), v in sorted(priced.items())],
                "totals": totals,
            }
        )
    )


if __name__ == "__main__":
    parse(Path(sys.argv[1]), Path(sys.argv[2]))
