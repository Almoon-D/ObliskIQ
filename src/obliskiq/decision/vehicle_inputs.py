"""The vehicle selector's market and calendar inputs (MASTER_PLAN §5.8), read the same way by the
core's rebalancer and ``obliskiq universe vehicles``: each vehicle's latest stored close and average
daily traded value, and each venue's overlap with the decision cycles. Tracking correlations come
from research history and are passed in by the caller (DECISIONS D64, D120).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Iterable
from datetime import date
from decimal import Decimal

from ..calendars.service import Calendars
from ..config.schema import ObliskConfig
from ..store import repos as market_repo
from ..store.db import Store
from .vehicle_selector import VehicleMarket, WindowOverlap, window_overlap


def market_inputs(
    store: Store, conids: Iterable[int], *, as_of: date, sessions: int
) -> dict[int, VehicleMarket]:
    """Each conid's latest close and average daily traded value over ``sessions`` stored bars."""
    out: dict[int, VehicleMarket] = {}
    for conid in conids:
        bars = market_repo.bar_series(store, conid, as_of=as_of, limit=sessions)
        if not bars:
            continue
        traded = [
            Decimal(repr(bar.close)) * Decimal(repr(bar.volume))
            for bar in bars
            if bar.volume is not None
        ]
        average = sum(traded, Decimal(0)) / len(traded) if traded else None
        out[conid] = VehicleMarket(Decimal(repr(bars[-1].close)), average, len(traded))
    return out


def windows(
    settings: ObliskConfig, calendars: Calendars, venues: Iterable[str], today: date
) -> dict[str, WindowOverlap]:
    """Each venue's overlap with the decision cycles on its next session from ``today``."""
    out: dict[str, WindowOverlap] = {}
    for venue in sorted(set(venues)):
        day = (
            today
            if calendars.session_bounds(venue, today)
            else calendars.next_session(venue, today)
        )
        if day is not None:
            out[venue] = window_overlap(settings, calendars, venue, day)
    return out
