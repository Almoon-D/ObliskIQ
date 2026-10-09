"""``obliskiq-compute`` — the computation layer as a standalone entrypoint.

Separate from both the collectors and the MCP server. It touches **no network**:
every engine here reads the store the collectors filled and writes derived
tables back. That is what makes the whole layer runnable and testable in a
sandbox with no IBKR access, and it is why a grid build can be scheduled beside
the collectors without a second set of credentials.

Subcommands::

    obliskiq-compute grid        market x sector x the fifteen indicator families
    obliskiq-compute rankings    the four ranked views and the correlation matrix
    obliskiq-compute fx          the four-way decomposition per market
    obliskiq-compute commodity   curves, wrappers, positioning, inventory, proxies
    obliskiq-compute census      the weekly all-gates count
    obliskiq-compute all         grid, rankings, fx and commodity, in order

``rankings`` reads the grid, so ``all`` runs the grid first. Exit codes match the
collectors: 0 everything reported ok, 1 something failed, 2 a run was degraded.
A grid whose coverage falls below ``grid.min_coverage_pct`` is DEGRADED, not
failed — most cells are legitimately absent on a young store, and a scheduler
should be able to tell "still filling up" from "broken".

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from datetime import date

from ..collect.runlog import CollectorRun, RunStatus, reap_interrupted_runs
from ..config import Config, load_config
from ..env import Settings, load_settings
from ..errors import ObliskError
from ..logging_setup import configure_logging, get_logger
from ..store import Store, open_store
from ..util.timeutil import parse_date, utc_today
from .build_grid import GridBuilder, family_reach
from .commodity_engine import CommodityEngine
from .fx_engine import FxEngine
from .iv_solve import SolveInputs, SolveReport, solve_session
from .rankings import CorrelationMatrix, Rankings
from .universe_census import UniverseCensus

log = get_logger("compute.cli")

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_DEGRADED = 2

COMMANDS = ("solve-iv", "grid", "rankings", "fx", "commodity", "census", "all")

#: Order matters: rankings read what the grid wrote, and the grid reads the
#: implied vols solve-iv wrote. Solving after the grid would publish a grid built
#: on the PREVIOUS run's solves, which is a whole session stale for no reason.
ALL_SEQUENCE = ("solve-iv", "grid", "rankings", "fx", "commodity")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="obliskiq-compute",
        description=(
            "The computation layer for obliskiq-lite. Reads the store the collectors "
            "filled and writes the grid, the rankings and the derived engine state. "
            "Touches no network."
        ),
    )
    parser.add_argument("command", choices=list(COMMANDS))
    parser.add_argument(
        "--as-of",
        help="session date, YYYY-MM-DD. Defaults to today in UTC.",
    )
    parser.add_argument(
        "--horizon-days",
        type=int,
        help="return horizon for the fx decomposition; defaults to one calendar year",
    )
    parser.add_argument("--nav", type=float, help="NAV override for the census")
    parser.add_argument("--dry-run", action="store_true", help="compute and report, write nothing")
    parser.add_argument("--config", help="path to config.yaml; defaults to $OBLISK_CONFIG_PATH")
    parser.add_argument("--log-level", help="override OBLISK_LOG_LEVEL")
    parser.add_argument("--log-format", choices=["json", "text"], help="override OBLISK_LOG_FORMAT")
    return parser


def _run_grid(store: Store, config: Config, args: argparse.Namespace, run: CollectorRun) -> None:
    as_of = _as_of(args)
    builder = GridBuilder(store, config)
    report = builder.build(as_of=as_of, write=not args.dry_run)
    run.notes["coverage"] = {
        "cells": report.cells_written,
        "populated": report.populated,
        "absent": report.absent,
        "stale": report.stale,
        "inapplicable": report.inapplicable,
        "collectible": report.collectible,
    }
    print(report.summary())

    # The narrowest family, on the line after the totals. A grid can report
    # healthy coverage while a fifth of its families see a handful of cells,
    # because the totals average over families that reach everything. The census
    # prints the whole table; this is the pointer to it.
    if not args.dry_run:
        reach = family_reach(store, as_of=as_of)
        if reach:
            narrowest = reach[0]
            run.notes["narrowest_family"] = {
                "family": narrowest.family,
                "cells_reachable": narrowest.cells_reachable,
                "cells_total": narrowest.cells_total,
            }
            print(
                f"narrowest family {narrowest.describe()}. "
                "`obliskiq-compute census` prints all fifteen."
            )

    minimum = config.require_float("grid.min_coverage_pct")
    degraded = report.coverage_pct < minimum
    run.succeeded(
        "grid",
        report.cells_written,
        degraded=degraded,
        reason=(
            None
            if not degraded
            else (
                f"collection health is {report.populated}/{report.collectible} "
                f"({report.coverage_pct:.0%}) against a floor of {minimum:.0%}. The "
                "denominator counts only cells something is supposed to be able to fill; "
                f"the other {report.inapplicable} leaves are structurally inapplicable and "
                "are reported as the ambition gap, not as a collection failure. This is a "
                "report, not a failure."
            )
        ),
        populated=report.populated,
        absent=report.absent,
        stale=report.stale,
        inapplicable=report.inapplicable,
        collectible=report.collectible,
    )


def _run_rankings(
    store: Store, config: Config, args: argparse.Namespace, run: CollectorRun
) -> None:
    as_of = _as_of(args)
    rankings = Rankings(store, config)
    rows = rankings.build(as_of=as_of)
    if not args.dry_run:
        rankings.write(rows, as_of=as_of)
    run.succeeded("rankings", len(rows))

    builder = GridBuilder(store, config)
    matrix = CorrelationMatrix(store, config, builder.markets)
    cells = matrix.build(as_of=as_of)
    if not args.dry_run:
        matrix.write(cells, as_of=as_of)
    flagged = [cell for cell in cells if cell.flagged]
    run.succeeded("correlation_matrix", len(cells), flagged=len(flagged))
    print(
        f"{len(rows)} ranked rows across {len({row.dimension for row in rows})} dimensions; "
        f"{len(cells)} correlation cells, {len(flagged)} flagged beyond their own norm."
    )


def _run_fx(store: Store, config: Config, args: argparse.Namespace, run: CollectorRun) -> None:
    as_of = _as_of(args)
    horizon = args.horizon_days or round(config.require_float("numerics.bsm.day_count_basis"))
    builder = GridBuilder(store, config)
    engine = FxEngine(store, config, builder.markets)
    results = engine.decompose_all(as_of=as_of, horizon_days=horizon)
    if not args.dry_run:
        engine.write(results, as_of=as_of)
    rankable = engine.rank(results)
    run.succeeded("fx", len(results), rankable=len(rankable))
    print(
        f"{len(results)} markets decomposed over {horizon} days; {len(rankable)} rankable on "
        "base-real, "
        f"{sum(1 for row in results if row.flagged)} flagged for inflation or depreciation."
    )


def _run_commodity(
    store: Store, config: Config, args: argparse.Namespace, run: CollectorRun
) -> None:
    as_of = _as_of(args)
    engine = CommodityEngine(store, config)
    states = engine.state_all(as_of=as_of)
    proxies = [engine.proxy_quality(entry, as_of=as_of) for entry in engine.proxy_map()]
    if not args.dry_run:
        engine.write(states, as_of=as_of)
        engine.write_proxies(proxies, as_of=as_of)
    rejected = [state for state in states if state.wrapper.rejects_futures_tracking]
    unreliable = [proxy for proxy in proxies if not proxy.reliable]
    run.succeeded(
        "commodity",
        len(states),
        wrapper_rejections=len(rejected),
        unreliable_proxies=len(unreliable),
    )
    print(
        f"{len(states)} roots; {len(rejected)} reject a futures-tracking wrapper on contango; "
        f"{len(unreliable)} of {len(proxies)} proxies are below the R-squared floor."
    )


def _run_solve_iv(
    store: Store, config: Config, args: argparse.Namespace, run: CollectorRun
) -> None:
    """Solve an ATM implied vol per underlying where the route served none.

    Degraded rather than failed past the non-convergence ceiling: a chain too
    thin or too wide to invert is a fact about the book, not a broken run, and
    the grid says which cells it left dark.
    """
    as_of = _as_of(args)
    inputs = SolveInputs.from_config(config)
    report = solve_session(store, config, as_of=as_of) if not args.dry_run else SolveReport(as_of)
    degraded = report.is_degraded(inputs)
    run.succeeded(
        "solve-iv",
        report.solved,
        degraded=degraded,
        reason=report.summary(inputs),
        attempted_contracts=report.attempted_contracts,
        converged_contracts=report.converged_contracts,
        skipped=report.skipped,
        non_converged_reasons=report.reasons,
    )
    print(report.summary(inputs))


def _run_census(store: Store, config: Config, args: argparse.Namespace, run: CollectorRun) -> None:
    as_of = _as_of(args)
    census = UniverseCensus(store, config)
    result = census.run(as_of=as_of, nav=args.nav)
    if not args.dry_run:
        census.write(result)
    run.succeeded(
        "census",
        result.contracts_scanned,
        degraded=not result.is_viable,
        reason=result.reason,
        passed_all_gates=result.passed_all_gates,
    )
    print(result.report())


RUNNERS = {
    "solve-iv": _run_solve_iv,
    "grid": _run_grid,
    "rankings": _run_rankings,
    "fx": _run_fx,
    "commodity": _run_commodity,
    "census": _run_census,
}


def _as_of(args: argparse.Namespace) -> date:
    if not args.as_of:
        return utc_today()
    parsed = parse_date(args.as_of)
    if parsed is None:
        raise ObliskError(f"--as-of {args.as_of!r} is not a date in YYYY-MM-DD form")
    return parsed


def _exit_code(statuses: Sequence[str]) -> int:
    if any(status in (RunStatus.ERROR, RunStatus.PARTIAL) for status in statuses):
        return EXIT_FAILED
    if any(status == RunStatus.DEGRADED for status in statuses):
        return EXIT_DEGRADED
    return EXIT_OK


def run_command(command: str, store: Store, config: Config, args: argparse.Namespace) -> list[str]:
    """Run one command, or the whole ``all`` sequence with each step guarded.

    Guarded for the same reason the collector sequence is: a step that raises
    used to abandon every step after it, so a bug in the fx decomposition cost
    the commodity engine its run too. Nothing here is irreplaceable — every
    output is derived and recomputes tomorrow — but a partial ``all`` that
    reported only the exception is still a worse account than one that says which
    engines ran and which did not.
    """
    commands = ALL_SEQUENCE if command == "all" else (command,)
    statuses: list[str] = []
    for name in commands:
        try:
            with CollectorRun(store, f"compute:{name}", dry_run=args.dry_run) as run:
                RUNNERS[name](store, config, args, run)
        except Exception as exc:
            # Broad on purpose, as in collect.cli: the failure this guard
            # exists for is the one nobody anticipated, and narrowing it to
            # ObliskError would leave exactly those escaping.
            log.error(
                "compute step failed; the sequence continues with the steps after it",
                extra={"step": name, "error": repr(exc)},
            )
            statuses.append(RunStatus.ERROR)
            if len(commands) == 1:
                # Asked for directly, the caller named this one thing and did not
                # get it. Re-raised so main() reports it the way it always has.
                raise
            continue
        statuses.append(run.status())
    return statuses


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings: Settings = load_settings()
        config = load_config(args.config or settings.config_path)
        configure_logging(
            level=args.log_level or settings.log_level,
            fmt=args.log_format or settings.log_format,
            utc=config.require_bool("runtime.logging.utc_timestamps"),
        )
        with open_store(settings.db_path, config) as store:
            # A killed process cannot record its own death, so every entrypoint
            # closes out what the last one left open. See collect.runlog.
            reap_interrupted_runs(store, config)
            statuses = run_command(args.command, store, config, args)
    except ObliskError as exc:
        configure_logging()
        log.error("compute command could not run", extra={"error": str(exc)})
        return EXIT_FAILED

    log.info("compute finished", extra={"command": args.command, "statuses": statuses})
    return _exit_code(statuses)


if __name__ == "__main__":  # pragma: no cover - module entrypoint
    sys.exit(main())
