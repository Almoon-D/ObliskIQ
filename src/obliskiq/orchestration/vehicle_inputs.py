"""What the vehicle selector and the swap rules read, gathered from the stores for the live core
(MASTER_PLAN §5.8, DECISIONS D120, D140).

The selector is pure (``decision/vehicle_selector.py``); the caller reads the market store (prices
and traded value), the research store (tracking and partner correlations, ECB rates) and the
calendars, and passes them in. ``obliskiq universe vehicles`` does it for one exposure at the
command line; :class:`StoredRoleInputs` does it for the service. The live decision path never
imports research (DECISIONS D64), so the reading lives here, beside the assembly. A research store
that is missing leaves tracking and rates unmeasured, with the reason, never filled in.

:class:`EcbRates` is the rate history the trend tilt converts a past close with.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import asyncio
from collections.abc import Iterable, Mapping, Sequence
from datetime import date
from decimal import Decimal
from pathlib import Path

from ..calendars.service import Calendars
from ..config.schema import ObliskConfig
from ..config.schema_universe import UniverseInstrument
from ..decision.vehicle_selector import (
    Exposure,
    SelectorInputs,
    VehicleMarket,
    WindowOverlap,
    window_overlap,
)
from ..quant.tracking import CorrelationReading
from ..research.data import FxRates, universe_series_id
from ..research.tracking import ResearchCorrelations, pair_readings, tracking_readings
from ..store import open_research_store
from ..store import repos as market_repo
from ..store.db import Store
from ..store.worker import StoreWorker

NO_RESEARCH = "no research store (`obliskiq research data load-universe` fills it)"


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


def base_rates(fx: FxRates, currencies: Iterable[str], base: str, on: date) -> dict[str, Decimal]:
    """The base currency's value of one unit of each currency on ``on``, where a fresh rate
    exists."""
    rates: dict[str, Decimal] = {}
    for currency in currencies:
        value = fx.to_base(1.0, currency, base, on)
        if value is not None:
            rates[currency] = Decimal(repr(value))
    return rates


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


def exposure_series(exposure: Exposure) -> str | None:
    if exposure.series is not None:
        return exposure.series
    return None if exposure.measured_by is None else universe_series_id(exposure.measured_by)


def _unmeasured(keys: Iterable[str], reason: str) -> dict[str, CorrelationReading]:
    return {key: CorrelationReading.unmeasured(reason) for key in keys}


def unmeasured(keys: Iterable[str], reason: str) -> dict[str, CorrelationReading]:
    return {key: CorrelationReading.unmeasured(reason) for key in keys}


class StoredRoleInputs:
    """The selector's inputs for the service, read from the stores."""

    def __init__(
        self,
        settings: ObliskConfig,
        *,
        worker: StoreWorker,
        calendars: Calendars,
        data_dir: Path,
    ) -> None:
        self._settings = settings
        self._worker = worker
        self._calendars = calendars
        self._data_dir = data_dir

    def _venue_of(self, declared: UniverseInstrument) -> str:
        return self._settings.ibkr_exchanges[declared.listing_exchange].venue

    def _research(self) -> Path | None:
        path = self._data_dir / self._settings.paths.research_database
        return path if path.exists() else None

    async def selector_inputs(
        self, exposure: Exposure, candidates: Sequence[UniverseInstrument], today: date
    ) -> SelectorInputs:
        settings = self._settings
        market = await self._worker.run(
            market_inputs,
            [i.conid for i in candidates if i.conid is not None],
            as_of=today,
            sessions=settings.vehicles.ranking.liquidity_sessions,
        )
        symbols = [i.symbol for i in candidates]
        currencies = (
            {i.currency for i in candidates}
            | {feed.monthly_cost.currency for feed in settings.market_data.feeds}
            | {s.currency for s in settings.costs.commissions}
        )
        tracking, rates = await asyncio.to_thread(
            self._research_inputs, exposure, symbols, currencies, today
        )
        return SelectorInputs(
            as_of=today,
            rates=rates,
            tracking=tracking,
            windows=windows(
                settings, self._calendars, {self._venue_of(i) for i in candidates}, today
            ),
            market=market,
        )

    def _research_inputs(
        self, exposure: Exposure, symbols: Sequence[str], currencies: set[str], today: date
    ) -> tuple[Mapping[str, CorrelationReading], dict[str, Decimal]]:
        if self._research() is None:
            return unmeasured(symbols, NO_RESEARCH), {}
        with open_research_store(self._settings, self._data_dir) as store:
            research = ResearchCorrelations(store, self._settings, as_of=today)
            series = {symbol: universe_series_id(symbol) for symbol in symbols}
            tracking = tracking_readings(research, exposure_series(exposure), series)
            rates = base_rates(research.fx, currencies, self._settings.account.base_currency, today)
        return tracking, rates

    async def partner_readings(
        self, pairs: Sequence[tuple[str, str]], today: date
    ) -> Mapping[tuple[str, str], CorrelationReading]:
        return await asyncio.to_thread(self._pair_readings, pairs, today)

    def _pair_readings(
        self, pairs: Sequence[tuple[str, str]], today: date
    ) -> Mapping[tuple[str, str], CorrelationReading]:
        if self._research() is None:
            return {pair: CorrelationReading.unmeasured(NO_RESEARCH) for pair in pairs}
        symbols = {symbol for pair in pairs for symbol in pair}
        series = {symbol: universe_series_id(symbol) for symbol in symbols}
        with open_research_store(self._settings, self._data_dir) as store:
            research = ResearchCorrelations(store, self._settings, as_of=today)
            return pair_readings(research, pairs, series)


class EcbRates:
    """ECB reference rates from the research store, to convert a past close into the base
    currency. Load them once for a day (off the event loop), then ask."""

    def __init__(self, settings: ObliskConfig, *, data_dir: Path) -> None:
        self._settings = settings
        self._data_dir = data_dir
        self._base = settings.account.base_currency
        self._fx: FxRates | None = None
        self._loaded: date | None = None

    async def load(self, as_of: date) -> None:
        if self._loaded == as_of:
            return
        self._fx = await asyncio.to_thread(self._read, as_of)
        self._loaded = as_of

    def _read(self, as_of: date) -> FxRates | None:
        if not (self._data_dir / self._settings.paths.research_database).exists():
            return None
        with open_research_store(self._settings, self._data_dir) as store:
            return ResearchCorrelations(store, self._settings, as_of=as_of).fx

    def to_base(self, amount: Decimal, currency: str, on: date) -> Decimal | None:
        if self._fx is None:
            return None
        local, target = self._fx.per_euro(currency, on), self._fx.per_euro(self._base, on)
        if local is None or target is None:
            return None
        return amount * Decimal(repr(target)) / Decimal(repr(local))
