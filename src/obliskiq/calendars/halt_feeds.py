"""Structured halt feeds read into instrument incidents. Network-free.

:func:`structured_items` is market-pulse-bot's structured-feed reader (``halt_detector.py``
``_structured_items``): a JSON list (or an object holding ``items`` or ``data``), else XML whose
``item`` elements are flattened to their children's text by local name. A document that declares
a DTD or an entity is refused unparsed.

:func:`nasdaq_trader_incidents` reads the Nasdaq Trader trade-halts feed: each halt of an
instrument we hold a conid for, on a venue whose feed code ``markets`` maps, becomes an
instrument-scoped feed incident from the halt to its resumption (open-ended while the feed shows
no resumption). A single-stock halt blocks that instrument only, never its venue (DECISIONS
D125); market-wide halts stay manual. Halts of anything else are counted and left out.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import json
import re
import xml.etree.ElementTree as ET
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from ..domain.calendar import Incident, IncidentScope, IncidentSource
from ..errors import UpstreamError

#: Declarations that let a document expand or fetch entities.
_UNSAFE = re.compile(rb"<!(DOCTYPE|ENTITY)", re.IGNORECASE)
_PUNCTUATION = re.compile(r"[^A-Z0-9]")
DATE_FORMAT = "%m/%d/%Y"
TIME_FORMATS = ("%H:%M:%S.%f", "%H:%M:%S")
STAMP_FORMAT = "%Y%m%dT%H%M%SZ"


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def structured_items(content: bytes) -> list[dict[str, str]]:
    """The items of a structured feed, each a mapping of field name to text."""
    try:
        decoded = json.loads(content)
    except ValueError:
        decoded = None
    if decoded is not None:
        items = decoded.get("items", decoded.get("data")) if isinstance(decoded, dict) else decoded
        if isinstance(items, list) and all(isinstance(item, dict) for item in items):
            return [{str(k): "" if v is None else str(v) for k, v in i.items()} for i in items]
        raise UpstreamError("the feed's JSON holds no list of items")
    if _UNSAFE.search(content):
        raise UpstreamError("the feed declares a DTD or an entity; not parsed")
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise UpstreamError(f"the feed is neither JSON nor well-formed XML: {exc}") from exc
    return [
        {_local(child.tag): (child.text or "").strip() for child in item}
        for item in root.iter()
        if _local(item.tag) == "item"
    ]


def instrument_key(symbol: str) -> str:
    """A ticker without punctuation or case: IBKR writes ``BRK B``, a feed ``BRK.B``."""
    return _PUNCTUATION.sub("", symbol.upper())


def _instant(day: str, clock: str, zone: ZoneInfo) -> datetime | None:
    if not day.strip() or not clock.strip():
        return None
    try:
        date_part = datetime.strptime(day.strip(), DATE_FORMAT).date()
    except ValueError:
        return None
    for fmt in TIME_FORMATS:
        try:
            time_part = datetime.strptime(clock.strip(), fmt).time()
        except ValueError:
            continue
        return datetime.combine(date_part, time_part, tzinfo=zone).astimezone(UTC)
    return None


@dataclass(frozen=True, slots=True)
class HaltRead:
    """What one read of a halt feed yielded: the incidents, and how many halts were left out
    and why."""

    incidents: tuple[Incident, ...]
    left_out: Counter[str] = field(default_factory=Counter)
    listed: int = 0


def nasdaq_trader_incidents(
    items: list[dict[str, str]],
    *,
    origin: str,
    markets: Mapping[str, str],
    zone: ZoneInfo,
    conids: Mapping[tuple[str, str], int],
) -> HaltRead:
    """The incidents of the instruments we follow. ``conids`` maps (venue, :func:`instrument_key`)
    to a conid; ``zone`` is the feed's (US Eastern)."""
    incidents: dict[str, Incident] = {}
    left_out: Counter[str] = Counter()
    for item in items:
        venue = markets.get(item.get("Market", "").strip())
        if venue is None:
            left_out["market not followed"] += 1
            continue
        symbol = item.get("IssueSymbol", "").strip()
        conid = conids.get((venue, instrument_key(symbol))) if symbol else None
        if conid is None:
            left_out["instrument not followed"] += 1
            continue
        start = _instant(item.get("HaltDate", ""), item.get("HaltTime", ""), zone)
        if start is None:
            left_out["unreadable halt time"] += 1
            continue
        end = _instant(item.get("ResumptionDate", ""), item.get("ResumptionTradeTime", ""), zone)
        code = item.get("ReasonCode", "").strip() or "unstated"
        name = item.get("IssueName", "").strip() or symbol
        incident_id = f"{symbol}@{venue}:{start.strftime(STAMP_FORMAT)}"
        incidents[incident_id] = Incident(
            incident_id=incident_id,
            origin=origin,
            source=IncidentSource.FEED,
            scope=IncidentScope.INSTRUMENT,
            venue=venue,
            conid=conid,
            start=start,
            expected_end=end if end is not None and end > start else None,
            reason=f"single-stock halt, reason code {code} ({name})",
        )
    return HaltRead(tuple(incidents.values()), left_out, len(items))
