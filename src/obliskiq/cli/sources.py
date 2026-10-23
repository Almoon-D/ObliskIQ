"""``obliskiq sources``: the data sources besides IBKR, by hand.

``list`` shows every declared source with its consumers, release-date policy and schedule (or the
condition that would build it); ``health`` each built source's state; ``run`` runs one built
source (or ``all``) now; ``backfill`` loads the CFTC's annual archives; ``catalysts`` lists the
catalyst calendar ahead. None of them opens a broker session.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import argparse
import asyncio
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import TYPE_CHECKING, TextIO

from ..data.events.catalysts import catalyst_calendar
from ..data.events.earnings import yfinance_earnings
from ..data.market.plumbing import Collection
from ..data.runner import SourceRunner, client_for, source_health
from ..env import secret_reader
from ..errors import ObliskError
from ..orchestration.runlog import exit_code, verdict
from ..store import open_store
from ..store import sources as source_repo
from ..store.worker import StoreWorker
from ..util.timeutil import utc_now
from .collect import report_collection

if TYPE_CHECKING:
    from .app import Loaded

EXIT_OK = 0
EXIT_INVALID = 1
ALL = "all"


def _sources_list(loaded: Loaded, out: TextIO) -> int:
    for name, provider in loaded.config.settings.sources.providers.items():
        print(f"{name} [{provider.area}, {provider.status}] {provider.name}", file=out)
        if provider.status == "built":
            moments = ", ".join(
                f"{m.venue} {m.anchor}{m.offset_minutes:+d}m" for m in provider.schedule
            )
            print(f"  consumers: {', '.join(provider.consumers)}", file=out)
            print(
                f"  release: {provider.release_date_policy} (+{provider.release_lag_days} d); "
                f"stale after {provider.stale_after_hours} h; runs: {moments or 'heartbeat'}",
                file=out,
            )
        else:
            print(f"  activation: {provider.activation}", file=out)
        if provider.credential is not None:
            print(f"  credential: {provider.credential}", file=out)
        for note in provider.bias_notes:
            print(f"  bias: {note}", file=out)
    return EXIT_OK


def _sources_health(loaded: Loaded, out: TextIO) -> int:
    settings = loaded.config.settings
    with open_store(settings, loaded.env.require_data_dir()) as store:
        runs = source_repo.source_runs(store)
    for found in source_health(settings, runs, now=utc_now()):
        success = "never" if found.last_success_at is None else found.last_success_at.isoformat()
        print(
            f"{found.source:<14} {found.state.value:<8} last success {success}, "
            f"{found.rows} rows" + (f": {found.reason}" if found.reason else ""),
            file=out,
        )
    return EXIT_OK


def _with_runner(
    loaded: Loaded, work: Callable[[SourceRunner], Awaitable[list[Collection]]]
) -> list[Collection]:
    settings = loaded.config.settings
    data_dir = loaded.env.require_data_dir()

    async def go() -> list[Collection]:
        worker = StoreWorker(lambda: open_store(settings, data_dir))
        try:
            async with client_for(settings) as client:
                runner = SourceRunner(
                    settings,
                    client=client,
                    worker=worker,
                    secrets=secret_reader(loaded.env),
                    now=utc_now,
                    sleep=asyncio.sleep,
                    earnings=yfinance_earnings,
                )
                return await work(runner)
        finally:
            await worker.aclose()

    return asyncio.run(go())


def _sources_run(loaded: Loaded, out: TextIO) -> int:
    args = loaded.args

    async def work(runner: SourceRunner) -> list[Collection]:
        names = runner.runnable() if args.source == ALL else (args.source,)
        return [await runner.run(name, dry_run=args.dry_run, force=args.force) for name in names]

    try:
        collections = _with_runner(loaded, work)
    except ObliskError as exc:
        print(f"refused: {exc}", file=out)
        return EXIT_INVALID
    for collection in collections:
        report_collection(collection, out)
    status = verdict([c.judge() for c in collections])
    print(f"run: {status}", file=out)
    return exit_code(status)


def _sources_backfill(loaded: Loaded, out: TextIO) -> int:
    args = loaded.args

    async def work(runner: SourceRunner) -> list[Collection]:
        return [await runner.backfill(args.start, args.end, dry_run=args.dry_run)]

    (collection,) = _with_runner(loaded, work)
    report_collection(collection, out)
    return exit_code(collection.status)


def _sources_catalysts(loaded: Loaded, out: TextIO) -> int:
    settings = loaded.config.settings
    today = utc_now().date()
    days = loaded.args.days or settings.sources.catalysts.earnings.horizon_days
    with open_store(settings, loaded.env.require_data_dir()) as store:
        found = catalyst_calendar(store, settings, as_of=today, through=today + timedelta(days))
    if not found:
        print("no catalyst in the window", file=out)
    for catalyst in found:
        print(
            f"{catalyst.day.isoformat()} {catalyst.kind:<14} {catalyst.subject:<10} "
            f"[{catalyst.grade}] {catalyst.source}",
            file=out,
        )
    return EXIT_OK


def add_sources_commands(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    sources = commands.add_parser("sources", help="the data sources besides IBKR")
    actions = sources.add_subparsers(dest="action", required=True, metavar="ACTION")
    actions.add_parser("list", help="every source, built or declared").set_defaults(
        handler=_sources_list
    )
    actions.add_parser("health", help="each built source's health").set_defaults(
        handler=_sources_health
    )
    run = actions.add_parser("run", help="run a built source now")
    run.add_argument("source", help=f"a built source's name, or {ALL}")
    run.add_argument("--dry-run", action="store_true", help="read and write nothing")
    run.add_argument("--force", action="store_true", help="read again what is still fresh")
    run.set_defaults(handler=_sources_run)
    backfill = actions.add_parser("backfill", help="the CFTC's annual archives")
    backfill.add_argument("--from", dest="start", type=int, required=True, help="first year")
    backfill.add_argument("--to", dest="end", type=int, required=True, help="last year")
    backfill.add_argument("--dry-run", action="store_true", help="read and write nothing")
    backfill.set_defaults(handler=_sources_backfill)
    catalysts = actions.add_parser("catalysts", help="the catalyst calendar ahead")
    catalysts.add_argument("--days", type=int, help="how far ahead (default: the earnings horizon)")
    catalysts.set_defaults(handler=_sources_catalysts)
