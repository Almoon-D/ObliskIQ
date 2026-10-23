"""The sources besides IBKR, run by name and judged by their health.

:class:`SourceRunner` runs one built source (``obliskiq sources run``, or a scheduled job of the
service) through its collector, and records the run's verdict, rows and reason as the source's
health; a declared source is refused with the condition that would build it. Every request but
SEC's carries ``sources.user_agent``; a credential a source names is read by that name.

:func:`source_health` turns each source's last run into what the status reports: ``failing``
when its last run failed outright, ``stale`` when it has not succeeded within its
``stale_after_hours`` (or never has), ``ok`` otherwise.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import functools
from collections.abc import Callable, Mapping
from datetime import date, datetime, timedelta

import httpx
from pydantic import TypeAdapter

from ..config.schema import ObliskConfig
from ..domain.sources import RunVerdict, SourceHealth, SourceRun, SourceState
from ..errors import ConfigError
from ..orchestration.runlog import RunRecord, StepOutcome
from ..store import sources as source_repo
from ..store.worker import StoreWorker
from ..util.http import RetryPolicy
from .events.earnings import EarningsCollector, EarningsSource
from .events.gdelt import GdeltCollector
from .events.markets import PredictionMarkets
from .events.rss import NewsCollector
from .insiders.edgar import Form4Collector
from .macro.fetchers import build_fetchers
from .macro.pull import MacroPull
from .market.plumbing import Collection, Pacer, Sleep, failure_text
from .positioning.cot import CotCollector

#: A run that failed outright.
FAILED = "error"
#: The source read by the service's heartbeat, not by a job.
HEARTBEAT_SOURCES = frozenset({"nasdaq_trader"})
_VERDICT: TypeAdapter[RunVerdict] = TypeAdapter(RunVerdict)


def _judge(collection: Collection) -> StepOutcome:
    return collection.judge()


def client_for(settings: ObliskConfig) -> httpx.AsyncClient:
    """The HTTP client every source shares: ``sources.user_agent`` by default."""
    return httpx.AsyncClient(
        headers={"User-Agent": settings.sources.user_agent}, follow_redirects=True
    )


class SourceRunner:
    """Runs a built source by name and records its health."""

    def __init__(
        self,
        settings: ObliskConfig,
        *,
        client: httpx.AsyncClient,
        worker: StoreWorker,
        secrets: Callable[[str], str | None],
        now: Callable[[], datetime],
        sleep: Sleep,
        earnings: EarningsSource,
    ) -> None:
        self._settings = settings
        self._client = client
        self._worker = worker
        self._secrets = secrets
        self._now = now
        self._sleep = sleep
        self._earnings = earnings
        self._policy = RetryPolicy.from_settings(settings.http)
        self._pacer = Pacer(settings.collectors, sleep)

    def runnable(self) -> tuple[str, ...]:
        """The built sources a run can name (the halt feed is the heartbeat's)."""
        return tuple(
            name for name in self._settings.sources.built() if name not in HEARTBEAT_SOURCES
        )

    async def run(self, source: str, *, dry_run: bool = False, force: bool = False) -> Collection:
        """Run ``source`` once; unless ``dry_run``, its verdict becomes its recorded health. A
        collector that breaks is a failed run, recorded as one."""
        self._provider(source)
        try:
            collection = await self._collect(source, dry_run=dry_run, force=force)
        except Exception as exc:
            collection = Collection(source)
            collection.failed(source, failure_text(exc))
        if not dry_run:
            judged = collection.judge()
            await self._worker.run(
                source_repo.record_source_run,
                source,
                at=self._now(),
                status=_VERDICT.validate_python(judged.status),
                rows=collection.rows,
                detail=judged.reason or None,
            )
        return collection

    async def run_job(self, command: str, session_date: date, source: str) -> RunRecord:
        """A scheduled run of ``source`` as one run record of one step (the service's job)."""
        record = RunRecord(
            self._worker, command=command, session_date=session_date, steps=(source,), now=self._now
        )
        await record.begin()
        await record.step(source, functools.partial(self.run, source), _judge)
        await record.finish()
        return record

    async def backfill(self, start_year: int, end_year: int, *, dry_run: bool) -> Collection:
        """The CFTC's annual archives of the years given (the only source with a backfill)."""
        return await self._cot().backfill(start_year, end_year, dry_run=dry_run)

    def _provider(self, source: str) -> str:
        providers = self._settings.sources.providers
        provider = providers.get(source)
        if provider is None:
            raise ConfigError(f"no source named {source}; the sources are {', '.join(providers)}")
        if provider.status != "built":
            raise ConfigError(f"{source} is declared, not built: {provider.activation}")
        if source in HEARTBEAT_SOURCES:
            raise ConfigError(f"{source} is read by the service's heartbeat, not run by name")
        return provider.area

    def _secret(self, name: str | None) -> str | None:
        return None if name is None else self._secrets(name)

    def _cot(self) -> CotCollector:
        return CotCollector(
            self._settings,
            client=self._client,
            policy=self._policy,
            worker=self._worker,
            pacer=self._pacer,
            now=self._now,
        )

    async def _collect(self, source: str, *, dry_run: bool, force: bool) -> Collection:
        area = self._provider(source)
        settings, client, policy = self._settings, self._client, self._policy
        worker, now = self._worker, self._now
        if area == "macro":
            fetchers = build_fetchers(
                settings,
                client,
                policy,
                fred_api_key=self._secret(settings.sources.provider("fred").credential),
                eia_api_key=self._secret(settings.sources.provider("eia").credential),
            )
            pull = MacroPull(settings, fetchers=fetchers, pacer=self._pacer, worker=worker, now=now)
            return await pull.run(source, dry_run=dry_run, force=force)
        if source == "cftc":
            return await self._cot().run(dry_run=dry_run)
        if source == "sec_edgar":
            return await Form4Collector(
                settings,
                client=client,
                policy=policy,
                pacer=self._pacer,
                user_agent=self._secret(settings.sources.provider(source).credential),
                sleep=self._sleep,
                worker=worker,
                now=now,
            ).run(dry_run=dry_run)
        if source in ("polymarket", "kalshi"):
            markets = PredictionMarkets(
                settings, client=client, policy=policy, pacer=self._pacer, worker=worker, now=now
            )
            if source == "polymarket":
                return await markets.polymarket(dry_run=dry_run)
            return await markets.kalshi(dry_run=dry_run)
        if source == "gdelt":
            return await GdeltCollector(
                settings, client=client, policy=policy, sleep=self._sleep, worker=worker, now=now
            ).run(dry_run=dry_run)
        if source == "rss":
            agents = {
                name: self._secrets(name)
                for feed in settings.sources.news.feeds.values()
                if (name := feed.user_agent_env) is not None
            }
            return await NewsCollector(
                settings,
                client=client,
                policy=policy,
                pacer=self._pacer,
                agents=agents,
                worker=worker,
                now=now,
            ).run(dry_run=dry_run)
        if source == "earnings":
            return await EarningsCollector(
                settings, pacer=self._pacer, fetch=self._earnings, worker=worker, now=now
            ).run(dry_run=dry_run, force=force)
        raise ConfigError(f"{source} is built but no collector runs it")  # pragma: no cover


def source_health(
    settings: ObliskConfig, runs: Mapping[str, SourceRun], *, now: datetime
) -> list[SourceHealth]:
    """The health of every built source, in the order config lists them."""
    health: list[SourceHealth] = []
    for name, provider in settings.sources.built().items():
        run = runs.get(name)
        window = timedelta(hours=provider.stale_after_hours or 0)
        if run is None:
            state, reason = SourceState.STALE, "never run"
        elif run.last_status == FAILED:
            state = SourceState.FAILING
            reason = f"{run.consecutive_failures} failed runs in a row: {run.detail or 'no detail'}"
        elif run.last_success_at is None or now - run.last_success_at > window:
            state = SourceState.STALE
            reason = f"no success within {provider.stale_after_hours} hours"
        else:
            state, reason = SourceState.OK, run.detail or ""
        health.append(
            SourceHealth(
                source=name,
                state=state,
                last_attempt_at=None if run is None else run.last_attempt_at,
                last_success_at=None if run is None else run.last_success_at,
                rows=0 if run is None else run.last_rows,
                reason=reason,
            )
        )
    return health
