"""The compute run: the session's implied-vol solve, the measurement build, the ranked views and
the correlation matrix, and the census, each a guarded step of one run record.

Ported in shape from lite's ``compute/cli.py``. **It touches no network**: every step reads the
store and writes derived rows back (an import contract and a test with every socket refused say
so), which is what makes it runnable beside the collectors without credentials. Exit codes are
the collectors': 0 ok, 1 a step failed, 2 a run was degraded. A young store is degraded, not
failed: most leaves are legitimately absent, and a scheduler must be able to tell "still filling
up" from "broken".

* ``solve-iv``: one solved ATM implied vol per underlying the route priced without one; degraded
  past ``numerics.implied_vol.solve.max_non_converged_fraction``.
* ``measure``: the equity grid, the macro board and the per-instrument forecast and gap
  distributions, written together; degraded below ``grid.min_coverage_pct`` of the collectible
  equity leaves.
* ``rankings``: the cross-asset correlation matrix, written, and the four ranked views of the
  newest built grid; degraded when there is no grid to rank.
* ``census``: the option universe and family reach; degraded while the candidate gates are
  untested.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import functools
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Final

from ..calendars.service import Calendars
from ..config.schema import ObliskConfig
from ..grid.census import Census, CensusResult
from ..grid.correlations import CorrelationMatrix
from ..grid.equity_grid import EquityGridBuilder, GridReport, tally
from ..grid.instrument_measures import InstrumentMeasures
from ..grid.iv_session import SessionSolver
from ..grid.macro_board import MacroBoardBuilder
from ..grid.rankings import RankingRow, Rankings
from ..grid.views import family_reach
from ..quant.iv_solve import SolveInputs, SolveReport
from ..store.db import Store
from ..store.worker import StoreWorker
from .runlog import DEGRADED, OK, RunRecord, StepOutcome, exit_code

SOLVE_IV: Final = "solve-iv"
MEASURE: Final = "measure"
RANKINGS: Final = "rankings"
CENSUS: Final = "census"
ALL: Final = "all"
#: The steps of ``all``, in order: a grid reads the solve, the rankings read the grid.
COMPUTE_STEPS: Final = (SOLVE_IV, MEASURE, RANKINGS, CENSUS)
COMMAND: Final = "compute"
#: Rows of each ranked view a report prints.
REPORTED_ROWS: Final = 5


@dataclass(frozen=True, slots=True)
class MeasureResult:
    grid: GridReport
    board: GridReport
    instrument_leaves: int
    instruments: int
    narrowest: str | None
    written: int


@dataclass(frozen=True, slots=True)
class RankingsResult:
    rows: tuple[RankingRow, ...]
    pairs: Counter[str]
    written: int


@dataclass(slots=True)
class ComputeOutcome:
    """A run's verdict, its exit code and what it reports."""

    status: str
    exit_code: int
    lines: list[str] = field(default_factory=list)


class ComputeRunner:
    """Runs compute steps against one store worker, under one run record each."""

    def __init__(
        self,
        settings: ObliskConfig,
        worker: StoreWorker,
        *,
        calendars: Callable[[date], Calendars],
        now: Callable[[], datetime],
    ) -> None:
        self._settings = settings
        self._worker = worker
        self._calendars = calendars
        self._now = now

    async def run(self, step: str, *, as_of: date, dry_run: bool) -> ComputeOutcome:
        steps = COMPUTE_STEPS if step == ALL else (step,)
        record = RunRecord(
            self._worker,
            command=f"{COMMAND} {step}",
            session_date=as_of,
            steps=steps,
            now=self._now,
            dry_run=dry_run,
        )
        lines: list[str] = []
        await record.begin()
        for name in steps:
            if name == SOLVE_IV:
                await record.step(
                    name,
                    functools.partial(self._worker.run, self._solve, as_of, dry_run),
                    functools.partial(self._judge_solve, lines),
                )
            elif name == MEASURE:
                calendars = self._calendars(as_of)
                await record.step(
                    name,
                    functools.partial(self._worker.run, self._measure, as_of, dry_run, calendars),
                    functools.partial(self._judge_measure, lines),
                )
            elif name == RANKINGS:
                await record.step(
                    name,
                    functools.partial(self._worker.run, self._rankings, as_of, dry_run),
                    functools.partial(self._judge_rankings, lines),
                )
            else:
                await record.step(
                    name,
                    functools.partial(self._worker.run, self._census, as_of),
                    functools.partial(self._judge_census, lines),
                )
        failed = [outcome for outcome in record.outcomes if outcome.status not in (OK, DEGRADED)]
        lines += [f"{outcome.name}: {outcome.status} {outcome.reason}" for outcome in failed]
        status = await record.finish()
        lines.append(f"run: {status}")
        return ComputeOutcome(status, exit_code(status), lines)

    # ── the steps, on the store's thread ──

    def _solve(self, store: Store, as_of: date, dry_run: bool) -> SolveReport:
        return SessionSolver(store, self._settings).run(
            as_of=as_of, at=self._now(), write=not dry_run
        )

    def _measure(
        self, store: Store, as_of: date, dry_run: bool, calendars: Calendars
    ) -> MeasureResult:
        at = self._now()
        grid = EquityGridBuilder(store, self._settings)
        board = MacroBoardBuilder(store, self._settings)
        instruments = InstrumentMeasures(store, self._settings, calendars)
        grid_rows = grid.build(as_of=as_of, computed_at=at)
        board_rows = board.build(as_of=as_of, computed_at=at)
        instrument_rows = instruments.build(as_of=as_of, computed_at=at)
        written = 0
        if not dry_run:
            with store.transaction():
                written = grid.write(grid_rows) + board.write(board_rows)
                written += instruments.write(instrument_rows)
        reach = family_reach(grid_rows)
        macro = self._settings.universe.macro_board
        return MeasureResult(
            grid=grid.report(grid_rows, as_of=as_of),
            board=tally(
                as_of,
                board_rows,
                markets=len(macro.regions),
                sectors=len(macro.asset_classes),
                measured_cells=len({row.entity_id for row in board_rows if row.status.has_value}),
            ),
            instrument_leaves=len(instrument_rows),
            instruments=len({row.entity_id for row in instrument_rows}),
            narrowest=reach[0].describe() if reach else None,
            written=written,
        )

    def _rankings(self, store: Store, as_of: date, dry_run: bool) -> RankingsResult:
        matrix = CorrelationMatrix(store, self._settings)
        rows = matrix.build(as_of=as_of, computed_at=self._now())
        written = 0 if dry_run else matrix.write(rows)
        statuses: Counter[str] = Counter(
            row.status.value for row in rows if row.leaf.startswith("correlation_")
        )
        ranked = tuple(Rankings(self._settings).build(store, as_of=as_of))
        return RankingsResult(ranked, statuses, written)

    def _census(self, store: Store, as_of: date) -> CensusResult:
        return Census(self._settings).run(store, as_of=as_of)

    # ── the verdicts ──

    def _judge_solve(self, lines: list[str], report: SolveReport) -> StepOutcome:
        inputs = SolveInputs.from_config(self._settings)
        summary = report.summary(inputs)
        skipped = ", ".join(f"{count} {reason}" for reason, count in sorted(report.skipped.items()))
        lines.append(f"solve-iv: {summary}" + (f"; skipped: {skipped}" if skipped else ""))
        status = DEGRADED if report.is_degraded(inputs) else OK
        return StepOutcome(SOLVE_IV, status, summary, {"solved": report.solved})

    def _judge_measure(self, lines: list[str], result: MeasureResult) -> StepOutcome:
        lines.append(result.grid.summary())
        if result.narrowest is not None:
            lines.append(f"narrowest family: {result.narrowest}")
        board = result.board
        lines.append(
            f"macro board: {board.leaves} leaves, {board.populated} populated, {board.absent} n/a, "
            f"{board.stale} stale, {board.inapplicable} inapplicable"
        )
        lines.append(
            f"instruments: {result.instruments} measured, {result.instrument_leaves} leaves "
            "(the HAR-RV forecast and the overnight-gap distribution)"
        )
        floor = self._settings.grid.min_coverage_pct
        coverage = result.grid.coverage_pct
        thin = coverage < floor
        reason = (
            f"equity-grid coverage {coverage:.0%} is below grid.min_coverage_pct {floor:.0%}"
            if thin
            else f"equity-grid coverage {coverage:.0%}"
        )
        return StepOutcome(MEASURE, DEGRADED if thin else OK, reason, {"written": result.written})

    def _judge_rankings(self, lines: list[str], result: RankingsResult) -> StepOutcome:
        pairs = ", ".join(f"{count} {status}" for status, count in sorted(result.pairs.items()))
        lines.append(f"correlation matrix: {pairs or 'no pair'}")
        if not result.rows:
            lines.append("rankings: no ranked row (no built grid holds a measured ranked leaf)")
            return StepOutcome(RANKINGS, DEGRADED, "nothing to rank", {"written": result.written})
        by_view: dict[str, list[RankingRow]] = {}
        for row in result.rows:
            by_view.setdefault(row.dimension.value, []).append(row)
        for view, rows in by_view.items():
            lines.append(f"{view}: {rows[0].detail}")
            lines += [
                f"  {row.rank:>2}. {row.market}/{row.sector} {row.leaf} {row.value:+.4f} "
                f"[{row.status.value}]"
                for row in rows[:REPORTED_ROWS]
            ]
        return StepOutcome(
            RANKINGS, OK, f"{len(result.rows)} ranked rows", {"written": result.written}
        )

    @staticmethod
    def _judge_census(lines: list[str], result: CensusResult) -> StepOutcome:
        lines.append(result.report())
        return StepOutcome(CENSUS, DEGRADED, result.reason, {"verdict": result.verdict.value})
