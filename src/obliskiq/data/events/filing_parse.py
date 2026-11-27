"""What the SEC's filings say, read without the network (DECISIONS D189, D200): an 8-K header's
items and acceptance, a document's text, an initial public offering's terms on its pricing
prospectus, and the sections of an annual or quarterly report.

A term the text does not state is ``None``: an offer price, a share count or a lock-up is read
where the prospectus writes it, never estimated, and a lock-up nobody states has no end.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import html
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Final
from zoneinfo import ZoneInfo

from ...errors import UpstreamError

#: The header's acceptance stamp, in EDGAR's local time.
ACCEPTANCE_FORMAT: Final = "%Y%m%d%H%M%S"
_ITEM = re.compile(r"^<ITEMS>\s*(\S+)\s*$", re.MULTILINE)
_ACCEPTED = re.compile(r"^<ACCEPTANCE-DATETIME>\s*(\d{14})\s*$", re.MULTILINE)
_TAG = re.compile(r"<[^>]+>")
_HIDDEN = re.compile(r"(?is)<(script|style)[^>]*>.*?</\1>|<ix:header>.*?</ix:header>")
_SPACE = re.compile(r"\s+")
#: An initial public offering's cover says so.
_IPO = re.compile(
    r"(?i)this is (?:the|an|our) initial public offering|prior to this offering,? there has been "
    r"no public market"
)
_PRICE = re.compile(
    r"(?i)initial public offering price (?:per share )?(?:is|of) \$\s?([\d,]+(?:\.\d+)?)"
)
_SHARES = re.compile(r"([\d,]{5,}) Shares\b")
_DATED = re.compile(
    r"(?:Prospectus dated|The date of this prospectus is) ([A-Z][a-z]+ \d{1,2}, \d{4})"
)
_DATE_FORMAT: Final = "%B %d, %Y"
_LOCKUP = re.compile(r"(?i)lock-?up")
_DAYS = re.compile(r"(?i)\b(\d{2,3})[- ]days?\b")
_SENTENCE = re.compile(r"(?<=[.;])\s+(?=[A-Z“\"(])")
#: A report's item heading: not a cross-reference (which opens with a quotation mark or runs on
#: in lower case, "Item 8. in this report"), so its title starts with a capital.
_HEADING = re.compile(r"(?<![\u201c\u2018\"'])\b(?i:item)\s+(\d{1,2}[A-Ca-c]?)\s*\.\s*(?=[A-Z])")


def folder(accession: str) -> str:
    """The archive folder of a filing: its accession without dashes."""
    return accession.replace("-", "")


def parse_header(text: str, *, zone: ZoneInfo) -> tuple[tuple[str, ...], datetime | None]:
    """An SGML header's item numbers and its acceptance instant (in UTC)."""
    if "<SEC-HEADER>" not in text:
        raise UpstreamError("the filing's header has no <SEC-HEADER>")
    items = tuple(dict.fromkeys(_ITEM.findall(text)))
    found = _ACCEPTED.search(text)
    if found is None:
        return items, None
    local = datetime.strptime(found.group(1), ACCEPTANCE_FORMAT).replace(tzinfo=zone)
    return items, local.astimezone(UTC)


def html_text(raw: str) -> str:
    """A document's text: tags, scripts and the inline-XBRL header removed, entities decoded,
    white space collapsed."""
    return _SPACE.sub(" ", html.unescape(_TAG.sub(" ", _HIDDEN.sub(" ", raw)))).strip()


def primary_document(body: Any) -> str:
    """The largest HTML document a filing's ``index.json`` lists (EDGAR marks no primary one)."""
    try:
        items = body["directory"]["item"]
    except KeyError, TypeError:
        raise UpstreamError("the filing's index lists no documents") from None
    documents = [
        item
        for item in items
        if isinstance(item, dict)
        and str(item.get("name", "")).lower().endswith((".htm", ".html"))
        and "-index" not in str(item.get("name", ""))
    ]
    if not documents:
        raise UpstreamError("the filing's index lists no HTML document")

    def size(item: Mapping[str, Any]) -> int:
        try:
            return int(item.get("size") or 0)
        except ValueError:
            return 0

    return str(max(documents, key=size)["name"])


@dataclass(frozen=True, slots=True)
class Prospectus:
    """What a pricing prospectus states: whether it is an initial public offering's, and its
    terms where the text writes them."""

    ipo: bool
    price: Decimal | None = None
    shares: int | None = None
    lockup_days: int | None = None
    prospectus_day: date | None = None

    def lockup_ends(self, filed: date) -> date | None:
        """The day the shortest stated lock-up ends, from the prospectus's date (its filing day
        when the text states none); ``None`` when no lock-up is stated."""
        if self.lockup_days is None:
            return None
        return (self.prospectus_day or filed) + timedelta(days=self.lockup_days)


def _number(text: str) -> Decimal | None:
    try:
        return Decimal(text.replace(",", ""))
    except InvalidOperation:
        return None


def _lockup_days(text: str) -> int | None:
    """The shortest lock-up the text states: the day counts of its sentences about a lock-up."""
    days = [
        int(found)
        for sentence in _SENTENCE.split(text)
        if _LOCKUP.search(sentence)
        for found in _DAYS.findall(sentence)
    ]
    return min(days) if days else None


def parse_prospectus(text: str, *, cover_chars: int) -> Prospectus:
    """A pricing prospectus's text as an initial public offering's terms, or not one."""
    cover = text[:cover_chars]
    if _IPO.search(cover) is None:
        return Prospectus(ipo=False)
    price = _PRICE.search(cover)
    shares = _SHARES.search(cover)
    dated = _DATED.search(text)
    count = None if shares is None else _number(shares.group(1))
    return Prospectus(
        ipo=True,
        price=None if price is None else _number(price.group(1)),
        shares=None if count is None or count <= 0 else int(count),
        lockup_days=_lockup_days(text),
        prospectus_day=(
            None if dated is None else datetime.strptime(dated.group(1), _DATE_FORMAT).date()
        ),
    )


def report_sections(text: str, wanted: Mapping[str, tuple[str, str]]) -> dict[str, str | None]:
    """Each wanted section of a report's text, by name: ``(item, title)`` names its heading
    (``("1A", "Risk Factors")``). A heading appears in the contents and at the section; the
    section is the longest span from a heading to the next one. ``None`` when no heading of it
    is found."""
    headings = list(_HEADING.finditer(text))
    found: dict[str, str | None] = {}
    for name, (item, title) in wanted.items():
        best: str | None = None
        for index, heading in enumerate(headings):
            if heading.group(1).upper() != item.upper():
                continue
            if (
                not text[heading.end() : heading.end() + len(title) + 2]
                .strip()
                .lower()
                .startswith(title.lower())
            ):
                continue
            end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
            span = text[heading.start() : end].strip()
            if best is None or len(span) > len(best):
                best = span
        found[name] = best
    return found
