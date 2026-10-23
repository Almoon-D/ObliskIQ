"""Macro collection: each declared series of a source into ``macro``, dated by its release rule.

Ported from lite's ``collect/macro_pull.py``. Every series is stored at its native frequency with
both dates; readers align on the release date. What changed: the run is one source's (the
service schedules each source on its own cadence), its targets are a :class:`Collection`, the
refetch memory is ``source_targets``, and two guards run on every row:

* **units.** A source that reports units must report the declared ones (``sources.macro.series
  [].units``); anything else means the series changed meaning (a rebased index, a new unit) and
  the series fails rather than storing a second quantity under one id. A source that reports none
  stores the declared units.
* **frequency.** A row is stored with its series' native frequency; the one exception is FRED's
  commodity history aggregated to monthly when ``commodity_history.frequency`` asks for it.

A repeat inside ``refetch_after_seconds`` asks nothing (``cached``). A source whose series fail
``stop_after_consecutive_failures`` times in a row is abandoned for the run with one finding,
not one per series left. A live fetch that returned nothing is ``degraded``, never a clean ok:
that is what a decoder reading the wrong envelope looks like (lite).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import functools
from collections.abc import Callable, Collection as Wanted, Mapping
from datetime import date, datetime

from ...config.schema import ObliskConfig
from ...domain.macro import MacroObservation
from ...store import repos
from ...store import sources as source_repo
from ...store.worker import StoreWorker
from ...util.timeutil import years_before
from ..market.plumbing import Collection, Pacer, PermanentCollectionError, failure_text
from .catalog import MacroCatalog, SeriesSpec
from .fetchers import Fetched, Fetcher, Window

#: The frequency stored for an aggregated commodity history.
MONTHLY = "monthly"


class UnitsChangedError(PermanentCollectionError):
    """A source reported units other than the series declares; a retry cannot change that."""


class MacroPull:
    """Walks one source's declared series and writes their observations."""

    def __init__(
        self,
        settings: ObliskConfig,
        *,
        fetchers: Mapping[str, Fetcher],
        worker: StoreWorker,
        pacer: Pacer,
        now: Callable[[], datetime],
    ) -> None:
        self._macro = settings.sources.macro
        self._days_per_year = settings.numerics.bsm.day_count_basis
        self._catalog = MacroCatalog.from_settings(settings)
        self._fetchers = fetchers
        self._worker = worker
        self._pacer = pacer
        self._now = now

    @property
    def catalog(self) -> MacroCatalog:
        return self._catalog

    async def run(
        self,
        source: str,
        *,
        only: Wanted[str] | None = None,
        dry_run: bool = False,
        force: bool = False,
    ) -> Collection:
        """Collect ``source``'s series (only those in ``only``, when given)."""
        collection = Collection(source)
        specs = [
            spec for spec in self._catalog.of_source(source) if only is None or spec.id in only
        ]
        if not specs:
            collection.empty_reason = (
                f"no declared series of {source}"
                + (f" among {sorted(only)}" if only is not None else "")
            )
            return collection
        fetcher = self._fetchers.get(source)
        if fetcher is None:
            for spec in specs:
                collection.failed(spec.id, f"no fetcher is built for source {source}")
            return collection
        failures = 0
        stop = self._macro.collection.stop_after_consecutive_failures
        for spec in specs:
            if not force and await self._fresh(spec):
                collection.succeeded(spec.id, 0, cached=True)
                continue
            try:
                rows = await self._pacer.retry(
                    spec.id, functools.partial(self._one, fetcher, spec)
                )
            except Exception as exc:  # one series never stops the source
                failures += 1
                collection.failed(spec.id, failure_text(exc))
                if not dry_run:
                    await self._record(spec, ok=False, rows=0, detail=failure_text(exc))
                if failures >= stop:
                    collection.failed(
                        f"source:{source}",
                        f"abandoned after {failures} failed series in a row; its remaining series "
                        "were not attempted",
                    )
                    break
                continue
            failures = 0
            if not dry_run:
                await self._worker.run(repos.upsert_macro, rows)
                await self._record(spec, ok=True, rows=len(rows), detail=None)
            collection.succeeded(
                spec.id,
                len(rows),
                degraded=not rows,
                reason=(
                    "the source answered and no observation survived decoding: it serves nothing "
                    "for this key, or its shape is not the one read (docs/knowledge/"
                    "macro-sources.md)"
                    if not rows
                    else ""
                ),
                dated=sum(1 for row in rows if row.release_date is not None),
            )
            await self._pacer.pace()
        return collection

    async def _fresh(self, spec: SeriesSpec) -> bool:
        succeeded = await self._worker.run(
            source_repo.target_succeeded_at, spec.source, spec.id
        )
        if succeeded is None:
            return False
        age = (self._now() - succeeded).total_seconds()
        return age < self._macro.collection.refetch_after_seconds

    async def _record(self, spec: SeriesSpec, *, ok: bool, rows: int, detail: str | None) -> None:
        await self._worker.run(
            source_repo.record_target,
            spec.source,
            spec.id,
            at=self._now(),
            ok=ok,
            rows=rows,
            detail=detail,
        )

    def window(self, spec: SeriesSpec, today: date) -> Window:
        years = (
            self._macro.commodity_history.window_years
            if spec.is_commodity_history
            else self._macro.collection.default_window_years
        )
        return Window(
            start=years_before(today, years, days_per_year=self._days_per_year), today=today
        )

    async def _one(self, fetcher: Fetcher, spec: SeriesSpec) -> list[MacroObservation]:
        now = self._now()
        fetched = await fetcher.fetch(spec, self.window(spec, now.date()))
        return self.observations(spec, fetched, now)

    def frequency_of(self, spec: SeriesSpec) -> str:
        """The frequency a series' rows are stored with (never a resampling of our own)."""
        history = self._macro.commodity_history
        if spec.source == "fred" and spec.is_commodity_history and history.frequency == MONTHLY:
            return MONTHLY
        return spec.frequency

    def observations(
        self, spec: SeriesSpec, fetched: list[Fetched], now: datetime
    ) -> list[MacroObservation]:
        """The rows to store: valued, dated by the series' rule, units checked, newest kept."""
        frequency = self.frequency_of(spec)
        rows: list[MacroObservation] = []
        for row in fetched:
            if row.units is not None and row.units != spec.units:
                raise UnitsChangedError(
                    f"{spec.id}: {spec.source} reports units {row.units!r}, the series declares "
                    f"{spec.units!r}; a changed unit is a different quantity under the same id"
                )
            if row.value is None:
                continue
            rows.append(
                MacroObservation(
                    series_id=spec.id,
                    source=spec.source,
                    reference_date=row.reference_date,
                    release_date=spec.release_date_for(
                        row.reference_date, from_api=row.release_date, collected_on=now.date()
                    ),
                    value=row.value,
                    frequency=frequency,
                    units=spec.units,
                    collected_at=now,
                )
            )
        cap = self._macro.collection.max_observations_per_series
        return rows[-cap:]
