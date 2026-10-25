"""The core's tracking and swap-partner correlations, read from research history for the service
(MASTER_PLAN §5.8; DECISIONS D64, D120).

The vehicle selector ranks a vehicle by how closely it tracks its role's series, and a swap
partner must correlate with its primary above ``vehicles.swap_partners.min_correlation``; both are
measured on research history (``research/tracking.py``), which the live decision path never reads.
The service reads them here, once a day, on the research store's own thread, for every core role:
each candidate against the role's series and each pair of the role's candidates. Without a
research store every reading is unmeasured, with the remedy.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from datetime import date
from pathlib import Path

from ..config.schema import ObliskConfig
from ..decision.core_vehicles import Correlations, MeasuredCorrelations, Unmeasured
from ..decision.vehicle_selector import resolve_exposure
from ..quant.tracking import CorrelationReading
from ..research.data import universe_series_id
from ..research.tracking import ResearchCorrelations, pair_readings, tracking_readings
from ..store import open_research_store
from ..store.db import Store
from ..store.worker import StoreWorker

#: What a reading says without a research store, and the remedy.
NO_RESEARCH_STORE = "no research store (`obliskiq research data load-universe` fills it)"


def measure(store: Store, settings: ObliskConfig, *, as_of: date) -> MeasuredCorrelations:
    """Every core role's tracking and partner correlations as of ``as_of``."""
    research = ResearchCorrelations(store, settings, as_of=as_of)
    by_exposure: dict[str, dict[str, CorrelationReading]] = {}
    by_pair: dict[tuple[str, str], CorrelationReading] = {}
    for role in settings.core.roles:
        exposure = resolve_exposure(settings, role)
        symbols = [
            declared.symbol
            for declared in settings.universe.instruments
            if exposure.held_by(declared)
        ]
        series = {symbol: universe_series_id(symbol) for symbol in symbols}
        by_exposure[role] = tracking_readings(research, exposure.series, series)
        pairs = [(a, b) for a in symbols for b in symbols if a != b]
        by_pair.update(pair_readings(research, pairs, series))
    return MeasuredCorrelations(by_exposure, by_pair, "not read for this role's candidates")


class ResearchCorrelationSource:
    """The research store's correlations, read once a day; unmeasured without the store."""

    def __init__(self, settings: ObliskConfig, data_dir: Path) -> None:
        self._settings = settings
        path = data_dir / settings.paths.research_database
        self._worker = (
            StoreWorker(lambda: open_research_store(settings, data_dir)) if path.exists() else None
        )
        self._read: tuple[date, Correlations] | None = None

    async def read(self, today: date) -> Correlations:
        if self._worker is None:
            return Unmeasured(NO_RESEARCH_STORE)
        if self._read is None or self._read[0] != today:
            found = await self._worker.run(measure, self._settings, as_of=today)
            self._read = (today, found)
        return self._read[1]

    async def aclose(self) -> None:
        if self._worker is not None:
            await self._worker.aclose()
