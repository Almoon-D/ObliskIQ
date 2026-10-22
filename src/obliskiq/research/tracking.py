"""Tracking and swap-partner correlations measured on research history (MASTER_PLAN §5.4, §5.8).

The vehicle selector ranks a vehicle by how closely it tracks its exposure's data series, and a
swap partner must correlate with the vehicle it replaces above ``vehicles.swap_partners.
min_correlation``. Both are read here, from the research store, point in time: every series is
bounded by what was public on ``as_of`` (``research_observations``) and by the window
``vehicles.correlation.lookback_days`` before it, converted to the base currency at ECB reference
rates (a day without a fresh rate is left out, never interpolated), and measured by
:func:`obliskiq.quant.tracking.return_correlation` over ``vehicles.correlation.period``.

A vehicle's series is its universe listing's dividend-adjusted close (``obliskiq research data
load-universe``); an exposure's is the series its role names (``core.roles``) or, for a grid cell,
the measuring instrument's listing. A series never loaded, or without a currency, is a reading
without a value and with the reason. The live decision path never imports this module (DECISIONS
D64): the caller passes the readings to the selector.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Iterable, Mapping
from datetime import date, timedelta

from ..config.schema import ObliskConfig
from ..config.schema_research import EcbDataset
from ..quant.tracking import CorrelationReading, Point, return_correlation
from ..store import Store
from ..store.research import research_observations, research_series_info
from .data import FxRates


class ResearchCorrelations:
    """Correlations between research series as of one date, each series read once."""

    def __init__(self, store: Store, settings: ObliskConfig, *, as_of: date) -> None:
        self._store = store
        self._settings = settings
        self._as_of = as_of
        config = settings.vehicles.correlation
        self._since = as_of - timedelta(days=config.lookback_days)
        self._min_returns = config.min_returns
        self._period = config.period
        self._base = settings.account.base_currency
        dataset = settings.research.dataset(config.fx_dataset)
        currencies = dataset.currencies if isinstance(dataset, EcbDataset) else []
        self._fx = FxRates.from_store(
            store,
            config.fx_dataset,
            [*currencies, self._base],
            as_of=as_of,
            max_age_days=settings.research.data.max_fx_age_days,
        )
        self._series: dict[str, tuple[list[Point], str | None]] = {}

    @property
    def fx(self) -> FxRates:
        """The ECB reference rates the series are converted with, as of the reading's date."""
        return self._fx

    def series(self, series_id: str) -> tuple[list[Point], str | None]:
        """The series in the base currency inside the window, or why it cannot be read."""
        if series_id not in self._series:
            self._series[series_id] = self._read(series_id)
        return self._series[series_id]

    def _read(self, series_id: str) -> tuple[list[Point], str | None]:
        info = research_series_info(self._store, series_id)
        if info is None:
            return [], f"{series_id} is not in the research store"
        if info.currency is None:
            return [], f"{series_id} states no currency to convert"
        observations = research_observations(self._store, series_id, as_of=self._as_of)
        points = [(o.observed_on, o.value) for o in observations if self._since <= o.observed_on]
        if not points:
            return [], f"{series_id} holds no value from {self._since} to {self._as_of}"
        if info.currency != self._base:
            points = self._fx.convert(points, info.currency, self._base)
            if not points:
                return [], (
                    f"{series_id} has no day with a fresh ECB rate for {info.currency} from "
                    f"{self._since} to {self._as_of}"
                )
        return points, None

    def correlation(self, first: str, second: str) -> CorrelationReading:
        """The correlation of two series' returns in the base currency."""
        a, why_a = self.series(first)
        if why_a is not None:
            return CorrelationReading.unmeasured(why_a)
        b, why_b = self.series(second)
        if why_b is not None:
            return CorrelationReading.unmeasured(why_b)
        return return_correlation(a, b, period=self._period, min_returns=self._min_returns)


def tracking_readings(
    correlations: ResearchCorrelations, exposure_series: str | None, vehicles: Mapping[str, str]
) -> dict[str, CorrelationReading]:
    """Each vehicle's correlation with the exposure's series, by symbol (``vehicles``: symbol to
    its research series)."""
    if exposure_series is None:
        reason = "the exposure names no data series"
        return {symbol: CorrelationReading.unmeasured(reason) for symbol in vehicles}
    return {
        symbol: correlations.correlation(series, exposure_series)
        for symbol, series in vehicles.items()
    }


def pair_readings(
    correlations: ResearchCorrelations,
    pairs: Iterable[tuple[str, str]],
    series: Mapping[str, str],
) -> dict[tuple[str, str], CorrelationReading]:
    """The correlation of each pair of vehicles, by their symbols (``series``: symbol to its
    research series)."""
    return {(a, b): correlations.correlation(series[a], series[b]) for a, b in pairs}
