"""One collection job: the universe, bars, snapshots, chains and curves of a set of venues, as one
run record of guarded steps.

Ported from lite's ``collect/sequence.py`` (``DAILY_SEQUENCE``) onto the service's run log
(``orchestration/runlog.py``): the run's row and its planned steps go in before any step works,
each step's row before the step, each verdict as it lands, and the run's verdict is the worst
step's. Steps are isolated: a step that raises, or outruns its ``collectors.step_deadline_seconds``
budget, is that step's ``error`` and the steps after it still run (the chain step must run after a
backfill that broke, because a chain not taken today is gone). The order is lite's: the universe
first (what is seeded is what is collected), bars before chains (a chain's strike band is sized
from the bars), curves last.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import asyncio
from collections.abc import Awaitable, Callable
from collections.abc import Collection as Venues
from datetime import date, datetime

from ...calendars.service import Calendars
from ...config.schema import ObliskConfig
from ...config.schema_collectors import COLLECT_STEPS, CollectStep
from ...domain.enums import InstrumentRole
from ...ibkr.client import IbkrClient
from ...orchestration.runlog import RunRecord, StepOutcome
from ...store import repos
from ...store.worker import StoreWorker
from .backfill import Backfill
from .chains import ChainCollector
from .curves import CurveCollector
from .plumbing import Collection, Pacer, Sleep
from .snapshots import SnapshotCollector
from .targets import (
    MarketTarget,
    RootTarget,
    candidate_targets,
    declared_targets,
    root_targets,
)
from .universe import UniverseSync

#: The run log's name for a collection job of one venue (``collect:<MIC>``) or a manual one.
COLLECT_COMMAND = "collect"


class StepDeadlineError(TimeoutError):
    """A step that outran its budget."""


class MarketCollector:
    """The collectors, wired once over an open IBKR client and the store."""

    def __init__(
        self,
        settings: ObliskConfig,
        *,
        client: IbkrClient,
        worker: StoreWorker,
        calendars: Calendars,
        now: Callable[[], datetime],
        sleep: Sleep,
    ) -> None:
        self._settings = settings
        self._worker = worker
        self._now = now
        pacer = Pacer(settings.collectors.retry, sleep)
        secdef, marketdata = client.secdef, client.marketdata
        self.universe = UniverseSync(settings, secdef=secdef, worker=worker, pacer=pacer, now=now)
        self.backfill = Backfill(
            settings,
            history=client.history,
            worker=worker,
            pacer=pacer,
            calendars=calendars,
            now=now,
        )
        self.snapshots = SnapshotCollector(
            settings, marketdata=marketdata, worker=worker, calendars=calendars, now=now
        )
        self.chains = ChainCollector(
            settings,
            secdef=secdef,
            marketdata=marketdata,
            worker=worker,
            pacer=pacer,
            calendars=calendars,
            now=now,
        )
        self.curves = CurveCollector(
            settings,
            secdef=secdef,
            marketdata=marketdata,
            worker=worker,
            pacer=pacer,
            calendars=calendars,
            now=now,
        )

    async def targets(self, venues: Venues[str] | None) -> list[MarketTarget]:
        """The seeded, standing instruments to collect on ``venues`` (every venue: ``None``)."""
        standing = await self._worker.run(
            repos.list_instruments, as_of=self._now().date(), include_superseded=False
        )
        seeded = {instrument.conid for instrument in standing}
        declared = [t for t in declared_targets(self._settings) if t.conid in seeded]
        candidates = candidate_targets(
            self._settings,
            (i for i in standing if InstrumentRole.TRADABLE_CANDIDATE in i.roles),
            (t.conid for t in declared_targets(self._settings)),
        )
        return [t for t in [*declared, *candidates] if venues is None or t.venue in venues]

    def roots(self, venues: Venues[str] | None) -> list[RootTarget]:
        return [r for r in root_targets(self._settings) if venues is None or r.venue in venues]

    async def step(
        self, step: CollectStep, venues: Venues[str] | None, *, dry_run: bool = False
    ) -> Collection:
        """One step over ``venues``, bounded by its deadline."""
        budget = self._settings.collectors.step_deadline_seconds[step]
        work: Awaitable[Collection]
        if step == "universe":
            work = self.universe.run(venues, dry_run=dry_run)
        elif step == "curves":
            work = self.curves.run(self.roots(venues), dry_run=dry_run)
        elif step == "backfill":
            work = self.backfill.run(await self.targets(venues), dry_run=dry_run)
        elif step == "snapshots":
            work = self.snapshots.run(await self.targets(venues), dry_run=dry_run)
        else:
            work = self.chains.run(await self.targets(venues), dry_run=dry_run)
        try:
            async with asyncio.timeout(budget):
                return await work
        except TimeoutError:
            raise StepDeadlineError(f"the {step} step outran its {budget} s budget") from None

    async def run(
        self,
        command: str,
        session_date: date,
        *,
        venues: Venues[str] | None,
        steps: tuple[CollectStep, ...] = COLLECT_STEPS,
        dry_run: bool = False,
    ) -> RunRecord:
        """The job as one run record; returns it finished."""
        record = RunRecord(
            self._worker,
            command=command,
            session_date=session_date,
            steps=steps,
            now=self._now,
            dry_run=dry_run,
        )
        record.notes["venues"] = sorted(venues) if venues is not None else "all"
        await record.begin()
        for name in steps:

            async def work(name: CollectStep = name) -> Collection:
                return await self.step(name, venues, dry_run=dry_run)

            await record.step(name, work, _judge)
        await record.finish()
        return record


def _judge(collection: Collection) -> StepOutcome:
    return collection.judge()
