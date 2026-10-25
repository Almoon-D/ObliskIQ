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
* ``surface``: the option surface per underlying (delta x tenor x implied vol, its percentile,
  skew, term slope, liquidity and theta); degraded when no underlying has a measured leaf.
* ``fx``: every grid market's four-way decomposition and carry test; degraded when no market
  ranks on base real.
* ``commodity``: every tracked root's curve, positioning, cycle and inventory, with its wrapper
  choice; degraded when no curve is classified.
* ``sleeve``: the options sleeve's candidates through the gates and its activation condition, its
  would-be entries logged while it runs in shadow; degraded when no contract was evaluated.
* ``census``: the option universe through the candidate gates and family reach; degraded below
  ``census.min_viable_contracts`` fully tested survivors.

The sleeve's budget is ``capital.budgets.option_premium`` of investable capital, which only the
account read gives, so in a compute run the pool's gates are untested (``OFFLINE_BUDGET``).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import functools
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Final

from ..calendars.service import Calendars
from ..config.schema import ObliskConfig
from ..domain.measurement import Measurement, MeasurementStatus
from ..engines.commodity.engine import CommodityEngine, WrapperChoice
from ..engines.fx.decomposition import FxEngine, carry_verdicts, fx_ranking
from ..engines.options_sleeve.sleeve import OptionsSleeve, SleeveRun
from ..grid.census import Census, CensusResult
from ..grid.correlations import CorrelationMatrix
from ..grid.equity_grid import EquityGridBuilder, GridReport, tally
from ..grid.instrument_measures import InstrumentMeasures
from ..grid.iv_session import SessionSolver
from ..grid.macro_board import MacroBoardBuilder
from ..grid.option_surface import OptionSurfaceBuilder
from ..grid.rankings import RankingRow, Rankings
from ..grid.views import family_reach
from ..quant.iv_solve import SolveInputs, SolveReport
from ..research.patterns import registered_hypothesis
from ..store.db import Store
from ..store.worker import StoreWorker
from .runlog import DEGRADED, OK, RunRecord, StepOutcome, exit_code

SOLVE_IV: Final = "solve-iv"
MEASURE: Final = "measure"
SURFACE: Final = "surface"
FX: Final = "fx"
COMMODITY: Final = "commodity"
RANKINGS: Final = "rankings"
SLEEVE: Final = "sleeve"
CENSUS: Final = "census"
ALL: Final = "all"
#: The steps of ``all``, in order: a grid reads the solve, the rankings read the grid, the sleeve
#: and the census read the surface's implied-vol percentile.
COMPUTE_STEPS: Final = (SOLVE_IV, MEASURE, SURFACE, FX, COMMODITY, RANKINGS, SLEEVE, CENSUS)
#: Why the pool's gates are untested in a compute run.
OFFLINE_BUDGET: Final = (
    "the sleeve's budget is capital.budgets.option_premium of investable capital, which only the "
    "account read gives; a compute run reads no account"
)
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


@dataclass(frozen=True, slots=True)
class LeafResult:
    """A build of measurements: how many leaves, how many measured, on how many entities."""

    rows: tuple[Measurement, ...]
    written: int

    @property
    def measured(self) -> int:
        return sum(1 for row in self.rows if row.status is MeasurementStatus.OK)

    @property
    def entities(self) -> int:
        return len({row.entity_id for row in self.rows})


@dataclass(frozen=True, slots=True)
class FxResult:
    leaves: LeafResult
    ranked: tuple[str, ...]
    carry: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CommodityResult:
    leaves: LeafResult
    wrappers: tuple[WrapperChoice, ...]


@dataclass(frozen=True, slots=True)
class SleeveResult:
    run: SleeveRun
    logged: int


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
        registry: Path | None = None,
    ) -> None:
        self._settings = settings
        self._worker = worker
        self._calendars = calendars
        self._now = now
        self._registry = registry

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
            elif name == SURFACE:
                await record.step(
                    name,
                    functools.partial(self._worker.run, self._surface, as_of, dry_run),
                    functools.partial(self._judge_surface, lines),
                )
            elif name == FX:
                await record.step(
                    name,
                    functools.partial(self._worker.run, self._fx, as_of, dry_run),
                    functools.partial(self._judge_fx, lines),
                )
            elif name == COMMODITY:
                await record.step(
                    name,
                    functools.partial(self._worker.run, self._commodity, as_of, dry_run),
                    functools.partial(self._judge_commodity, lines),
                )
            elif name == RANKINGS:
                await record.step(
                    name,
                    functools.partial(self._worker.run, self._rankings, as_of, dry_run),
                    functools.partial(self._judge_rankings, lines),
                )
            elif name == SLEEVE:
                calendars = self._calendars(as_of)
                await record.step(
                    name,
                    functools.partial(self._worker.run, self._sleeve, as_of, dry_run, calendars),
                    functools.partial(self._judge_sleeve, lines),
                )
            else:
                calendars = self._calendars(as_of)
                await record.step(
                    name,
                    functools.partial(self._worker.run, self._census, as_of, calendars),
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

    def _surface(self, store: Store, as_of: date, dry_run: bool) -> LeafResult:
        builder = OptionSurfaceBuilder(store, self._settings)
        rows = builder.build(as_of=as_of, computed_at=self._now())
        return LeafResult(tuple(rows), 0 if dry_run else builder.write(rows))

    def _fx(self, store: Store, as_of: date, dry_run: bool) -> FxResult:
        engine = FxEngine(store, self._settings)
        rows = engine.build(as_of=as_of, computed_at=self._now())
        written = 0 if dry_run else engine.write(rows)
        months = self._settings.grid.macro_board.change_horizons_months[0]
        ranked = (
            ()
            if dry_run
            else tuple(
                f"{row.market} {row.base_real:+.2%}"
                + (f" [{'; '.join(row.flags)}]" if row.flags else "")
                for row in fx_ranking(store, self._settings, as_of=as_of, months=months)
            )
        )
        carry = (
            ()
            if dry_run
            else tuple(
                f"{verdict.market}: {verdict.verdict}"
                for verdict in carry_verdicts(store, self._settings, as_of=as_of)
            )
        )
        return FxResult(LeafResult(tuple(rows), written), ranked, carry)

    def _commodity(self, store: Store, as_of: date, dry_run: bool) -> CommodityResult:
        engine = CommodityEngine(store, self._settings)
        rows = engine.build(as_of=as_of, computed_at=self._now())
        written = 0 if dry_run else engine.write(rows)
        wrappers = tuple(
            engine.wrapper(engine.curve_shape(root.root, as_of=as_of))
            for root in self._settings.universe.futures_roots
            if root.tracked
        )
        return CommodityResult(LeafResult(tuple(rows), written), wrappers)

    def _sleeve(
        self, store: Store, as_of: date, dry_run: bool, calendars: Calendars
    ) -> SleeveResult:
        hypothesis = self._settings.option_sleeve.activation.hypothesis
        evidence = (
            None if self._registry is None else registered_hypothesis(self._registry, hypothesis)
        )
        sleeve = OptionsSleeve(
            store, self._settings, calendars=calendars, evidence=evidence, now=self._now
        )
        run = sleeve.evaluate(as_of=as_of, investable=None, investable_gap=OFFLINE_BUDGET)
        return SleeveResult(run, 0 if dry_run else sleeve.log_shadow(run))

    def _census(self, store: Store, as_of: date, calendars: Calendars) -> CensusResult:
        return Census(self._settings).run(
            store, as_of=as_of, budget_gap=OFFLINE_BUDGET, calendars=calendars
        )

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
    def _judge_surface(lines: list[str], result: LeafResult) -> StepOutcome:
        summary = (
            f"option surface: {result.entities} underlyings, {len(result.rows)} leaves, "
            f"{result.measured} measured"
        )
        lines.append(summary)
        status = OK if result.measured else DEGRADED
        return StepOutcome(SURFACE, status, summary, {"written": result.written})

    @staticmethod
    def _judge_fx(lines: list[str], result: FxResult) -> StepOutcome:
        leaves = result.leaves
        summary = (
            f"fx: {leaves.entities} markets, {len(leaves.rows)} leaves, {leaves.measured} "
            f"measured; {len(result.ranked)} rank on base real"
        )
        lines.append(summary)
        lines += [f"  {line}" for line in result.ranked]
        lines += [f"  carry {line}" for line in result.carry]
        status = OK if result.ranked else DEGRADED
        return StepOutcome(FX, status, summary, {"written": leaves.written})

    @staticmethod
    def _judge_commodity(lines: list[str], result: CommodityResult) -> StepOutcome:
        leaves = result.leaves
        classified = sum(1 for choice in result.wrappers if choice.recommended is not None)
        summary = (
            f"commodity: {leaves.entities} roots, {len(leaves.rows)} leaves, {leaves.measured} "
            f"measured; {classified} curves classified"
        )
        lines.append(summary)
        lines += [
            f"  {choice.root}: {choice.recommended or 'no wrapper'}"
            + (f" (refuses {choice.rejected})" if choice.rejected else "")
            for choice in result.wrappers
        ]
        status = OK if classified else DEGRADED
        return StepOutcome(COMMODITY, status, summary, {"written": leaves.written})

    @staticmethod
    def _judge_sleeve(lines: list[str], result: SleeveResult) -> StepOutcome:
        run = result.run
        lines += run.report()
        lines.append(f"  shadow entries logged: {result.logged}")
        status = OK if run.scanned else DEGRADED
        return StepOutcome(
            SLEEVE,
            status,
            run.activation.reason(),
            {"logged": result.logged, "scanned": run.scanned},
        )

    @staticmethod
    def _judge_census(lines: list[str], result: CensusResult) -> StepOutcome:
        lines.append(result.report())
        status = OK if result.is_viable else DEGRADED
        return StepOutcome(CENSUS, status, result.reason, {"verdict": result.verdict.value})
