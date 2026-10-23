"""The catalyst calendar: central-bank decisions and key releases from config, each with the page
that states its date (``official``), and the universe's earnings dates as last read by the day
asked (``research``: unofficial, possibly estimated, never decisive alone).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from datetime import date

from ...config.schema import ObliskConfig
from ...domain.enums import AssetClass
from ...domain.sources import Catalyst
from ...store import Store
from ...store import sources as source_repo

EARNINGS = "earnings"


def catalyst_calendar(
    store: Store, settings: ObliskConfig, *, as_of: date, through: date
) -> list[Catalyst]:
    """The catalysts dated from ``as_of`` to ``through``, as known on ``as_of``, by day."""
    found = [
        Catalyst(
            kind=event.kind,
            subject=event.region,
            day=event.day,
            source=str(event.source),
            grade="official",
        )
        for event in settings.sources.catalysts.events
        if as_of <= event.day <= through
    ]
    for instrument in settings.universe.instruments:
        if instrument.asset_class is not AssetClass.STOCK:
            continue
        for listed in source_repo.earnings_dates(store, instrument.symbol, as_of=as_of):
            day = listed.report_at.date()
            if as_of <= day <= through:
                found.append(
                    Catalyst(
                        kind=EARNINGS,
                        subject=listed.symbol,
                        day=day,
                        source=listed.source,
                        grade=listed.grade,
                    )
                )
    return sorted(found, key=lambda c: (c.day, c.kind, c.subject))
