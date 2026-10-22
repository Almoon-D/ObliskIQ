"""What the collectors walk: the universe's pinned instruments and the stock candidates a thesis
pinned, each with the venue whose close schedules it, and the futures roots.

A declared instrument is a target only once the universe step has seeded it into the store (its
identity confirmed by IBKR's contract details) and while no correction has superseded it; a
leveraged or inverse product is never one (it is rejected at ingestion). An instrument without a
pin is not collected: ``obliskiq universe pin`` resolves it and writes its conid.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from ...calendars.service import Calendars
from ...config.schema import ObliskConfig
from ...config.schema_universe import FuturesRootConfig, UniverseInstrument
from ...domain.enums import AssetClass
from ...domain.instrument import Instrument, leveraged_inverse_marker
from ...errors import CalendarError

DECLARED = "declared"
CANDIDATE = "candidate"


@dataclass(frozen=True, slots=True)
class MarketTarget:
    """One instrument to collect, and how."""

    conid: int
    symbol: str
    listing_exchange: str
    history_exchange: str | None
    option_exchange: str | None
    currency: str
    asset_class: AssetClass
    #: The MIC whose close schedules this target, and its time zone.
    venue: str
    zone: ZoneInfo
    collect_bars: bool
    collect_chain: bool
    inception: date | None
    origin: str


@dataclass(frozen=True, slots=True)
class RootTarget:
    """One futures root whose curve is collected (data only)."""

    root: FuturesRootConfig
    venue: str
    zone: ZoneInfo


def venue_session(calendars: Calendars, venue: str, zone: ZoneInfo, now: datetime) -> date:
    """The session a collection at ``now`` is of: the venue's last completed one, or the venue's
    own date (in its ``zone``) where the calendars do not cover it."""
    try:
        completed = calendars.last_completed_session(venue, now)
    except CalendarError:
        completed = None
    return completed or now.astimezone(zone).date()


def venue_zone(settings: ObliskConfig, venue: str) -> ZoneInfo:
    return ZoneInfo(settings.venues[venue].timezone)


def venue_of(settings: ObliskConfig, exchange: str) -> str | None:
    entry = settings.ibkr_exchanges.get(exchange)
    return None if entry is None else entry.venue


def refused_as_leveraged(settings: ObliskConfig, declared: UniverseInstrument) -> str | None:
    """The marker that makes ``declared`` a leveraged or inverse product, if its description
    carries one."""
    if declared.description is None:
        return None
    return leveraged_inverse_marker(
        declared.description, settings.instruments.leveraged_inverse_markers
    )


def declared_targets(settings: ObliskConfig) -> tuple[MarketTarget, ...]:
    """Every pinned instrument of the universe that is not refused as leveraged or inverse."""
    targets: list[MarketTarget] = []
    for declared in settings.universe.instruments:
        venue = venue_of(settings, declared.listing_exchange)
        if declared.conid is None or venue is None or refused_as_leveraged(settings, declared):
            continue
        targets.append(
            MarketTarget(
                conid=declared.conid,
                symbol=declared.symbol,
                listing_exchange=declared.listing_exchange,
                history_exchange=declared.history_exchange,
                option_exchange=declared.option_exchange,
                currency=declared.currency,
                asset_class=declared.asset_class,
                venue=venue,
                zone=venue_zone(settings, venue),
                collect_bars=declared.collect_bars,
                collect_chain=declared.collects_chain,
                inception=declared.attributes.inception,
                origin=DECLARED,
            )
        )
    return tuple(targets)


def candidate_targets(
    settings: ObliskConfig, instruments: Iterable[Instrument], declared: Iterable[int]
) -> tuple[MarketTarget, ...]:
    """The pinned stock candidates the store holds, less any the universe declares itself."""
    known = set(declared)
    targets: list[MarketTarget] = []
    for instrument in instruments:
        venue = venue_of(settings, instrument.listing_exchange)
        if instrument.conid in known or venue is None:
            continue
        targets.append(
            MarketTarget(
                conid=instrument.conid,
                symbol=instrument.symbol,
                listing_exchange=instrument.listing_exchange,
                history_exchange=instrument.history_exchange,
                option_exchange=None,
                currency=instrument.currency,
                asset_class=instrument.asset_class,
                venue=venue,
                zone=venue_zone(settings, venue),
                collect_bars=True,
                collect_chain=False,
                inception=None,
                origin=CANDIDATE,
            )
        )
    return tuple(targets)


def root_targets(settings: ObliskConfig) -> tuple[RootTarget, ...]:
    """The tracked futures roots, each on the venue of its exchange."""
    return tuple(
        RootTarget(root, venue, venue_zone(settings, venue))
        for root in settings.universe.futures_roots
        if root.tracked and (venue := venue_of(settings, root.exchange)) is not None
    )


def collection_venues(settings: ObliskConfig) -> tuple[str, ...]:
    """The venues the universe declares something to collect on, instruments and roots."""
    venues = {t.venue for t in declared_targets(settings)} | {
        r.venue for r in root_targets(settings)
    }
    return tuple(sorted(venues))
