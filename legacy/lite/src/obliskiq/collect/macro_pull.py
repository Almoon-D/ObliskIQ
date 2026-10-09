"""Macro collection: FRED, ECB, CFTC, EIA into ``macro``.

Every series at its **native** frequency, with both ``reference_date`` and
``release_date`` stored. Downstream aligns on release. Aligning a CPI print to its
reference month means every percentile derived from it knows the number weeks
before the market did, and that bias is invisible once it is baked in.

Caching
-------

``macro_fetch_log`` records when each series was last pulled. A repeat run inside
``macro.collection.refetch_after_seconds`` issues **no request at all** — running
a collector twice in one day is normal (a manual trigger after a failure, say)
and must not spend the rate limit twice.

Windows
-------

Most series go back ``macro.collection.default_window_years``. The commodity
history group goes back ``commodities.history.monthly_window_years`` instead,
because a 5-year percentile is misleading on an asset whose cycle runs 10-20
years: oil has multi-decade supercycles and uranium spent fifteen years in a bear
market.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

from ..config import Config
from ..errors import NoMatchingTargets
from ..logging_setup import get_logger
from ..macro.fetchers import Fetcher
from ..macro.series import Observation, SeriesSpec, load_series, load_source_policies
from ..store import Store
from ..util.timeutil import age_seconds, utc_stamp, utc_today, years_before
from .retry import TargetRetryPolicy, pace, with_target_retry
from .runlog import CollectorRun

log = get_logger("collect.macro")

COLLECTOR_NAME = "macro"

STATUS_OK = "ok"
STATUS_ERROR = "error"


class MacroPull:
    """Walks the declared series and writes observations."""

    def __init__(
        self,
        store: Store,
        config: Config,
        fetchers: dict[str, Fetcher],
        *,
        today: date | None = None,
    ) -> None:
        self._store = store
        self._config = config
        self._fetchers = fetchers
        self._today = today or utc_today()
        self._policy = TargetRetryPolicy.from_config(config)

        collection = config.section("macro.collection")
        self._default_window_years = collection.require_int("default_window_years")
        self._refetch_after = collection.require_float("refetch_after_seconds")
        self._max_observations = collection.require_int("max_observations_per_series")
        self._calendar_days_per_year = config.require_float("numerics.bsm.day_count_basis")
        self._commodity_window_years = config.require_int(
            "commodities.history.monthly_window_years"
        )
        self._stop_after = config.require_int("collectors.macro.stop_after_consecutive_failures")

        self._series = load_series(config)
        self._policies = load_source_policies(config)

    async def run(
        self,
        run: CollectorRun,
        *,
        only: Sequence[str] | None = None,
        dry_run: bool = False,
        force: bool = False,
    ) -> int:
        wanted = {s.upper() for s in only} if only else None
        if wanted is not None and not any(spec.id.upper() in wanted for spec in self._series):
            # Same discipline as the chain and curve collectors: a filter naming no
            # declared series must not produce a run that reports 'ok' over nothing.
            raise NoMatchingTargets(
                f"none of the requested series {sorted(wanted)} is declared under "
                "macro.series. Check the id, or drop --only."
            )
        written = 0
        # Per SOURCE, not per run. The guard's own message says a dead upstream
        # should not be walked through every remaining series — but counting
        # across every source made ANY dead upstream stop the rest. FRED is
        # first in the list and needs an API key, so an unset FRED_API_KEY
        # aborted the whole run before it reached CFTC and ECB, neither of
        # which needs a key at all. That is the difference between a macro layer
        # that is a quarter collectible without credentials and one that is not
        # collectible at all.
        consecutive_failures: dict[str, int] = {}
        abandoned: set[str] = set()

        for spec in self._series:
            if wanted and spec.id.upper() not in wanted:
                continue
            source_policy = self._policies.get(spec.source)
            policy = None if source_policy is None else source_policy.for_series(spec)
            if policy is None:
                run.failed(spec.id, f"no macro.sources entry for source {spec.source!r}")
                continue
            if spec.source in abandoned:
                # Given up on earlier in this run, and recorded once when that
                # happened. Not recorded again per series: the point of the guard
                # is that a dead upstream produces one finding rather than fifty.
                log.debug(
                    "source abandoned earlier in this run; not attempting",
                    extra={"series": spec.id, "source": spec.source},
                )
                continue
            if not policy.enabled:
                log.info(
                    "source disabled; skipping", extra={"series": spec.id, "source": spec.source}
                )
                continue
            if not force and self._recently_fetched(spec):
                run.succeeded(spec.id, 0, cached=True, source=spec.source)
                continue
            fetcher = self._fetchers.get(spec.source)
            if fetcher is None:
                run.failed(spec.id, f"no fetcher registered for source {spec.source!r}")
                continue

            try:
                observations = await with_target_retry(
                    self._policy,
                    spec.id,
                    lambda f=fetcher, s=spec, p=policy: f.fetch(s, p, start=self._start_for(s)),
                )
                # Validation sits inside the guard on purpose: a series that comes
                # back resampled is one bad series, not a reason to abandon the
                # forty-nine after it in the list.
                rows = self._sane(spec, observations)
            except Exception as exc:
                failures = consecutive_failures.get(spec.source, 0) + 1
                consecutive_failures[spec.source] = failures
                self._log_fetch(spec, STATUS_ERROR, 0, repr(exc), dry_run=dry_run)
                run.failed(spec.id, repr(exc), source=spec.source)
                if failures >= self._stop_after and spec.source not in abandoned:
                    abandoned.add(spec.source)
                    # One finding for the source, not one per series left in it.
                    run.failed(
                        f"source:{spec.source}",
                        f"abandoned after {failures} consecutive failures; its remaining "
                        "series were not attempted. The other sources continued.",
                        source=spec.source,
                    )
                    log.error(
                        "abandoning this source: too many consecutive failures. A dead "
                        "upstream should not be walked through every remaining series — "
                        "and must not stop the sources that are answering.",
                        extra={"source": spec.source, "consecutive_failures": failures},
                    )
                continue

            consecutive_failures[spec.source] = 0
            if not dry_run:
                self._write(rows)
                self._log_fetch(spec, STATUS_OK, len(rows), None, dry_run=dry_run)
            written += len(rows)
            # A LIVE fetch that returned nothing is degraded, never a clean ok.
            # The target was attempted, the upstream answered, and no observation
            # survived — which is what a decoder reading the wrong envelope looks
            # like from here, and it read as success for as long as the OECD
            # fetcher was returning an empty list. It is not an error either: the
            # call worked, so failing the run would be as wrong in the other
            # direction. Degraded is the status that means "collected, on a
            # weaker input than intended", and this is exactly that.
            #
            # The cached path above reports zero rows too and stays ok on
            # purpose: it wrote nothing because the store already holds them.
            empty = not rows
            run.succeeded(
                spec.id,
                len(rows),
                degraded=empty,
                reason=(
                    "the upstream answered and no observation survived decoding. Either it "
                    "serves nothing for this key, or the response shape is not the one the "
                    "decoder reads — check the series key in config.yaml against "
                    "references/macro-playbook.md before assuming the former."
                    if empty
                    else None
                ),
                source=spec.source,
                frequency=spec.frequency,
                with_release_date=sum(1 for row in rows if row.release_date),
            )
            await pace(self._policy)
        return written

    # ── windows and caching ──

    def _start_for(self, spec: SeriesSpec) -> date:
        years = (
            self._commodity_window_years
            if spec.is_commodity_history
            else self._default_window_years
        )
        return years_before(self._today, years, days_per_year=self._calendar_days_per_year)

    def _recently_fetched(self, spec: SeriesSpec) -> bool:
        row = self._store.execute(
            "SELECT fetched_at, status FROM macro_fetch_log WHERE series_id = ? AND source = ?",
            (spec.id, spec.source),
        ).fetchone()
        if row is None or row["status"] != STATUS_OK:
            return False
        age = age_seconds(row["fetched_at"])
        if age is None:
            return False
        return age < self._refetch_after

    # ── validation ──

    def _sane(self, spec: SeriesSpec, observations: list[Observation]) -> list[Observation]:
        """Drop rows that would corrupt the series rather than storing them.

        The frequency check is the load-bearing one: a fetcher returning a
        frequency other than the declared native one means something resampled,
        and a resampled macro series propagates into every percentile built on it.
        """
        kept: list[Observation] = []
        for observation in observations:
            if observation.frequency != spec.frequency:
                raise ValueError(
                    f"{spec.id} came back at frequency {observation.frequency!r} but is "
                    f"declared as {spec.frequency!r}. Macro series are stored at their "
                    "native frequency and are never resampled."
                )
            if observation.value is None:
                continue
            kept.append(observation)
        if len(kept) > self._max_observations:
            log.warning(
                "series returned more observations than the configured maximum; truncating "
                "to the most recent",
                extra={"series": spec.id, "returned": len(kept), "cap": self._max_observations},
            )
            kept = kept[-self._max_observations :]
        return kept

    # ── writing ──

    def _write(self, observations: list[Observation]) -> None:
        if not observations:
            return
        stamp = utc_stamp()
        with self._store.transaction() as connection:
            connection.executemany(
                """
                INSERT INTO macro (series_id, source, reference_date, release_date, value,
                                   frequency, units, collected_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (series_id, source, reference_date) DO UPDATE SET
                    release_date = excluded.release_date,
                    value = excluded.value,
                    frequency = excluded.frequency,
                    units = excluded.units,
                    collected_at = excluded.collected_at
                """,
                [
                    (
                        observation.series_id,
                        observation.source,
                        observation.reference_date,
                        observation.release_date,
                        observation.value,
                        observation.frequency,
                        observation.units,
                        stamp,
                    )
                    for observation in observations
                ],
            )

    def _log_fetch(
        self, spec: SeriesSpec, status: str, count: int, detail: str | None, *, dry_run: bool
    ) -> None:
        if dry_run:
            return
        with self._store.transaction() as connection:
            connection.execute(
                "INSERT INTO macro_fetch_log (series_id, source, fetched_at, observations, "
                "status, detail) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (series_id, source) DO UPDATE SET "
                "fetched_at = excluded.fetched_at, observations = excluded.observations, "
                "status = excluded.status, detail = excluded.detail",
                (spec.id, spec.source, utc_stamp(), count, status, detail),
            )
