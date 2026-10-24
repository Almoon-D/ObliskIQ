"""``obliskiq compute``: the measurement store's steps, by hand.

``solve-iv`` solves the session's implied vols, ``measure`` builds the equity grid, the macro board
and the per-instrument measurements, ``surface`` the option surface, ``fx`` the four-way FX
decomposition and the carry test, ``commodity`` the commodity roots and their wrapper choice,
``rankings`` writes the correlation matrix and prints the ranked views, ``sleeve`` puts the options
sleeve's candidates through the gates and logs its would-be entries while it runs in shadow,
``census`` counts the option universe through the gates and family reach, and ``all`` runs them in
that order. ``--as-of`` names the session (today, UTC, by default), ``--dry-run`` computes and
reports without writing. Exit codes: 0 ok, 1 a step failed, 2 a run was degraded. The steps read
the store and write derived rows; none opens a connection.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import argparse
import asyncio
from datetime import date
from typing import TYPE_CHECKING, TextIO

from ..calendars.service import Calendars
from ..env import repository_root
from ..orchestration.compute import ALL, COMPUTE_STEPS, ComputeOutcome, ComputeRunner
from ..research.patterns import REGISTRY_PATH
from ..store import open_store
from ..store.worker import StoreWorker
from ..util.timeutil import utc_now

if TYPE_CHECKING:
    from .app import Loaded


def _compute(loaded: Loaded, out: TextIO) -> int:
    settings = loaded.config.settings
    data_dir = loaded.env.require_data_dir()
    args = loaded.args
    as_of: date = args.as_of if args.as_of is not None else utc_now().date()

    def calendars(reference: date) -> Calendars:
        return Calendars.from_settings(settings, reference=reference, data_dir=data_dir)

    root = repository_root()
    registry = None if root is None else root / REGISTRY_PATH

    async def go() -> ComputeOutcome:
        worker = StoreWorker(lambda: open_store(settings, data_dir))
        try:
            runner = ComputeRunner(
                settings, worker, calendars=calendars, now=utc_now, registry=registry
            )
            return await runner.run(args.step, as_of=as_of, dry_run=args.dry_run)
        finally:
            await worker.aclose()

    outcome = asyncio.run(go())
    for line in outcome.lines:
        print(line, file=out)
    return outcome.exit_code


def add_compute_commands(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    compute = commands.add_parser(
        "compute", help="build the measurement store and its views (touches no network)"
    )
    compute.add_argument("step", choices=[*COMPUTE_STEPS, ALL], help="what to compute")
    compute.add_argument(
        "--as-of", type=date.fromisoformat, default=None, help="the session (YYYY-MM-DD)"
    )
    compute.add_argument("--dry-run", action="store_true", help="compute and write nothing")
    compute.set_defaults(handler=_compute)
