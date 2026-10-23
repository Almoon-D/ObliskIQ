"""SEC Form 4 as EDGAR disseminates it: the full submission's header and its ownership document.

Network-free, so a research loader (should a later batch test H2, DECISIONS D84) and the live
collector share one parser. The submission (``<accession>.txt``) carries EDGAR's own header, with
the filing date that makes a filing public (``FILED AS OF DATE``) and the acceptance stamp, then
the ``ownershipDocument`` XML: the issuer, each reporting owner and their relationship, and the
transactions. A value the filer left to a footnote (a price, say) is ``None``, never zero; a
holding that reports no transaction is not a transaction.

The XML is the filer's text: a document that declares a DTD or an entity is refused before it is
parsed, and nothing in it is ever executed or followed.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import re
import xml.etree.ElementTree as ET
from datetime import datetime
from zoneinfo import ZoneInfo

from ...domain.sources import InsiderFiling, InsiderOwner, InsiderTransaction
from ...errors import UpstreamError
from ...util.numeric import safe_float
from ...util.timeutil import parse_date

_HEADER_FIELDS = {
    "accession": re.compile(r"^ACCESSION NUMBER:\s*(\S+)", re.MULTILINE),
    "form": re.compile(r"^CONFORMED SUBMISSION TYPE:\s*(\S+)", re.MULTILINE),
    "filed": re.compile(r"^FILED AS OF DATE:\s*(\d{8})", re.MULTILINE),
}
_ACCEPTED = re.compile(r"<ACCEPTANCE-DATETIME>(\d{14})")
_XML = re.compile(r"<XML>\s*(.*?)\s*</XML>", re.DOTALL)
#: Declarations that let a document expand or fetch entities.
_UNSAFE = re.compile(r"<!(DOCTYPE|ENTITY)", re.IGNORECASE)
_TRUE = frozenset({"1", "true"})
#: The acquired and disposed codes, and direct and indirect ownership.
ACQUIRED, DISPOSED = "A", "D"
DIRECT, INDIRECT = "D", "I"
STAMP_FORMAT = "%Y%m%d%H%M%S"


def _text(element: ET.Element | None, path: str) -> str | None:
    if element is None:
        return None
    found = element.find(path)
    if found is None or found.text is None:
        return None
    text = found.text.strip()
    return text or None


def _value(element: ET.Element, path: str) -> str | None:
    """A Form 4 field, which nests its text in ``<value>``."""
    return _text(element, f"{path}/value")


def _flag(text: str | None) -> bool:
    return (text or "").strip().lower() in _TRUE


def _choice(text: str | None, yes: str, no: str) -> bool | None:
    if text == yes:
        return True
    if text == no:
        return False
    return None


def _owners(root: ET.Element) -> tuple[InsiderOwner, ...]:
    owners: list[InsiderOwner] = []
    for owner in root.findall("reportingOwner"):
        cik = _text(owner, "reportingOwnerId/rptOwnerCik")
        name = _text(owner, "reportingOwnerId/rptOwnerName")
        if cik is None or not cik.isdigit() or name is None:
            raise UpstreamError("a Form 4 names a reporting owner without a CIK or a name")
        relation = owner.find("reportingOwnerRelationship")
        owners.append(
            InsiderOwner(
                owner_cik=int(cik),
                owner_name=name,
                is_director=_flag(_text(relation, "isDirector")),
                is_officer=_flag(_text(relation, "isOfficer")),
                is_ten_percent=_flag(_text(relation, "isTenPercentOwner")),
                is_other=_flag(_text(relation, "isOther")),
                officer_title=_text(relation, "officerTitle"),
            )
        )
    return tuple(owners)


def _transactions(root: ET.Element) -> tuple[InsiderTransaction, ...]:
    lines: list[InsiderTransaction] = []
    tables = (
        ("nonDerivativeTable/nonDerivativeTransaction", False),
        ("derivativeTable/derivativeTransaction", True),
    )
    for path, derivative in tables:
        for row in root.findall(path):
            day = parse_date(_value(row, "transactionDate"))
            code = _text(row, "transactionCoding/transactionCode")
            title = _value(row, "securityTitle")
            if day is None or code is None or title is None:
                raise UpstreamError("a Form 4 transaction lacks its date, code or security")
            lines.append(
                InsiderTransaction(
                    line=len(lines),
                    derivative=derivative,
                    security_title=title,
                    transaction_date=day,
                    code=code,
                    shares=safe_float(_value(row, "transactionAmounts/transactionShares"), None),
                    price=safe_float(
                        _value(row, "transactionAmounts/transactionPricePerShare"), None
                    ),
                    acquired=_choice(
                        _value(row, "transactionAmounts/transactionAcquiredDisposedCode"),
                        ACQUIRED,
                        DISPOSED,
                    ),
                    shares_after=safe_float(
                        _value(row, "postTransactionAmounts/sharesOwnedFollowingTransaction"),
                        None,
                    ),
                    direct=_choice(
                        _value(row, "ownershipNature/directOrIndirectOwnership"), DIRECT, INDIRECT
                    ),
                )
            )
    return tuple(lines)


def parse_submission(text: str, *, zone: ZoneInfo, collected_at: datetime) -> InsiderFiling:
    """One Form 4 submission (``<accession>.txt``) as a filing. ``zone`` is EDGAR's: its
    acceptance stamps are local time there."""
    header = {name: pattern.search(text) for name, pattern in _HEADER_FIELDS.items()}
    accepted = _ACCEPTED.search(text)
    document = _XML.search(text)
    missing = [name for name, found in header.items() if found is None]
    if missing or accepted is None or document is None:
        raise UpstreamError(
            "the submission lacks "
            + ", ".join(
                [
                    *missing,
                    *(["acceptance"] if accepted is None else []),
                    *(["its XML"] if document is None else []),
                ]
            )
        )
    xml = document.group(1)
    if _UNSAFE.search(xml):
        raise UpstreamError("the ownership document declares a DTD or an entity; not parsed")
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise UpstreamError(f"the ownership document is not well-formed XML: {exc}") from exc
    if root.tag != "ownershipDocument":
        raise UpstreamError(f"the submission's XML is a {root.tag}, not an ownershipDocument")
    issuer = _text(root, "issuer/issuerCik")
    if issuer is None or not issuer.isdigit():
        raise UpstreamError("the ownership document names no issuer CIK")
    filed = parse_date(_group(header["filed"]))
    if filed is None:
        raise UpstreamError("the submission's filing date is not a date")
    stamp = datetime.strptime(accepted.group(1), STAMP_FORMAT).replace(tzinfo=zone)
    return InsiderFiling(
        accession=_group(header["accession"]),
        form=_group(header["form"]),
        issuer_cik=int(issuer),
        issuer_symbol=_text(root, "issuer/issuerTradingSymbol"),
        filed_on=filed,
        accepted_at=stamp,
        period_of_report=parse_date(_text(root, "periodOfReport")),
        owners=_owners(root),
        transactions=_transactions(root),
        collected_at=collected_at,
    )


def _group(match: re.Match[str] | None) -> str:
    return "" if match is None else match.group(1)
