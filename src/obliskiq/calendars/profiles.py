"""Venue profiles: what trading on a venue needs beyond its calendar (MASTER_PLAN §5.3).

The currencies its listings trade in, the IBKR trading permission an order there needs and the
standard unit of its shares, each with where it comes from (``venues.yaml``). Market-data cost
and availability are the feeds of ``market_data`` (``data/market/availability.py``).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from ..config.schema import ObliskConfig


@dataclass(frozen=True, slots=True)
class VenueProfile:
    """One venue's profile; each ``*_source`` says where its value comes from and whether it
    still awaits a live reading."""

    mic: str
    name: str
    currencies: tuple[str, ...]
    currencies_source: str
    trading_permission: str
    permission_source: str
    board_lot: int | None
    lots_source: str
    unverified: tuple[str, ...]

    @property
    def local_currency(self) -> str:
        return self.currencies[0]


def venue_profiles(settings: ObliskConfig) -> Mapping[str, VenueProfile]:
    """Every configured venue's profile, by MIC."""
    profiles: dict[str, VenueProfile] = {}
    for mic, config in settings.venues.items():
        sources = config.sources
        unverified = tuple(
            name
            for name, provenance in (
                ("currencies", sources.currencies),
                ("permission", sources.permission),
                ("lots", sources.lots),
            )
            if provenance.verify_live
        )
        profiles[mic] = VenueProfile(
            mic=mic,
            name=config.name,
            currencies=tuple(config.currencies),
            currencies_source=sources.currencies.origin,
            trading_permission=config.trading_permission,
            permission_source=sources.permission.origin,
            board_lot=config.board_lot,
            lots_source=sources.lots.origin,
            unverified=unverified,
        )
    return MappingProxyType(profiles)
