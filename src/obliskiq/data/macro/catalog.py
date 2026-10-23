"""The declared macro series, the rule that dates each, and the guard that keeps an undatable
series out of a backtest. Network-free: the views and the research harness import it without an
HTTP client in their graph.

**The two dates.** ``reference_date`` is the period an observation describes; ``release_date``
when it became public. Readers align on release: a CPI print aligned to its reference month
lets every percentile derived from it know the number weeks before the market did. A release
date is never silently the reference date; each series follows its source's
``release_date_policy`` or its own (``sources.macro.series[].release``), and ``unknown`` stores
none (MASTER_PLAN §5.18; lite ``macro/series.py``).

**Backtests.** A series whose rule is ``unknown`` may feed a live view, never a backtest:
:func:`backtest_series` refuses it, as it refuses a series nobody declared.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Self

from ...config.schema import ObliskConfig
from ...config.schema_sources import Frequency, ReleaseDatePolicy, ValueVintage
from ...domain.macro import MacroSeries
from ...errors import ObliskError
from ...store import repos
from ...store.db import Store

#: The rules under which a series' history can be read point in time in a backtest.
BACKTEST_POLICIES: frozenset[ReleaseDatePolicy] = frozenset(
    {"from_api", "reference_plus_lag", "same_as_reference", "as_collected"}
)
#: The group whose window is the commodity history's rather than the default one.
COMMODITY_HISTORY_GROUP = "commodity_history"


class NotPointInTimeError(ObliskError):
    """A series a backtest may not read: undeclared, or without a defensible release date."""


@dataclass(frozen=True, slots=True)
class SeriesSpec:
    """One declared series and the release rule that applies to it."""

    id: str
    source: str
    frequency: Frequency
    units: str
    group: str
    key: str | None
    value_vintage: ValueVintage | None
    policy: ReleaseDatePolicy
    lag_days: int
    consumers: tuple[str, ...]

    @property
    def is_commodity_history(self) -> bool:
        return self.group == COMMODITY_HISTORY_GROUP

    @property
    def backtest_usable(self) -> bool:
        return self.policy in BACKTEST_POLICIES

    def release_date_for(
        self, reference: date, *, from_api: date | None = None, collected_on: date | None = None
    ) -> date | None:
        """The release date to store, or ``None`` when it is genuinely unknown.

        ``None`` is the point: a NULL release date is a coverage gap a reader can see; a
        reference date wearing a release date's name is look-ahead nobody can detect afterwards.
        """
        match self.policy:
            case "from_api":
                return from_api
            case "reference_plus_lag":
                return reference + timedelta(days=self.lag_days)
            case "same_as_reference":
                return reference
            case "as_collected":
                return collected_on
            case _:
                return None


@dataclass(frozen=True, slots=True)
class MacroCatalog:
    """Every declared series by id."""

    specs: Mapping[str, SeriesSpec]

    @classmethod
    def from_settings(cls, settings: ObliskConfig) -> Self:
        sources = settings.sources
        specs: dict[str, SeriesSpec] = {}
        for series in sources.macro.series:
            provider = sources.providers[series.source]
            if series.release is not None:
                policy, lag = series.release.policy, series.release.lag_days
            else:
                # A built source states both (the schema refuses one that does not).
                policy = provider.release_date_policy or "unknown"
                lag = provider.release_lag_days or 0
            specs[series.id] = SeriesSpec(
                id=series.id,
                source=series.source,
                frequency=series.frequency,
                units=series.units,
                group=series.group,
                key=series.key,
                value_vintage=series.value_vintage,
                policy=policy,
                lag_days=lag,
                consumers=tuple(series.consumers),
            )
        return cls(specs)

    def spec(self, series_id: str) -> SeriesSpec | None:
        return self.specs.get(series_id)

    def of_source(self, source: str) -> tuple[SeriesSpec, ...]:
        return tuple(spec for spec in self.specs.values() if spec.source == source)

    def require_backtest(self, series_id: str) -> SeriesSpec:
        """The spec of a series a backtest may read, or the reason it may not."""
        spec = self.specs.get(series_id)
        if spec is None:
            raise NotPointInTimeError(
                f"{series_id} is not declared in sources.macro.series: a series without a "
                "declared source and release rule never feeds a backtest"
            )
        if not spec.backtest_usable:
            raise NotPointInTimeError(
                f"{series_id} ({spec.source}) has release_date_policy {spec.policy}: without a "
                "defensible release date it may feed a live view, never a backtest"
            )
        return spec


def backtest_series(
    store: Store,
    catalog: MacroCatalog,
    series_id: str,
    *,
    as_of: date,
    since: date | None = None,
) -> MacroSeries:
    """A series as a backtest may read it on ``as_of``: only a declared series with a defensible
    release rule, aligned on its release dates (:func:`~obliskiq.store.repos.macro_series`)."""
    spec = catalog.require_backtest(series_id)
    return repos.macro_series(store, series_id, as_of=as_of, source=spec.source, since=since)
