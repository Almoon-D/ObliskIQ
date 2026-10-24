"""The ranked views: the session reads rankings, not thousands of raw leaves.

Ported from lite's ``Rankings`` (``compute/rankings.py``), as queries over one session's equity
cells. Four views, each ordered on ONE named leaf so the ordering can be checked against the cell
it came from (a composite score nobody can reproduce is what the design refuses):

``most_extended``               ``distance_from_52w_high``, nearest the high first
``most_compressed``             ``parkinson_vol_<tenor>d_percentile``, lowest first
``widest_iv_hv``                ``iv_minus_hv``, by magnitude (both tails matter)
``largest_correlation_shift``   ``correlation_shift_<window>d``, by magnitude, every window

``most_compressed`` orders on the Parkinson percentile: a rank of the range estimator against its
own history, where a bias common to the series cancels; the close-to-close percentile travels on
the row as a condition. The rest of SKILL.md's definitions travel as ``conditions`` (true, false,
or ``None`` where the store could not test one, which is never a failed one), and a chosen set of
the same cell's leaves travels as the ``trajectory``: a percentile is a level and says nothing
about whether it is still building or already unwinding. Every row carries what the view could
reach and what it ranked over; a view below ``grid.min_cells_for_ranked_view`` is labelled, never
suppressed. Ranked rows admit ``stale`` leaves and say so: a stale reading is still the best the
store holds.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import Final

from ..config.schema import ObliskConfig
from ..domain.enums import MarketDataStatus
from ..domain.measurement import EntityKind, Measurement, MeasurementStatus
from ..store import measurements as measurements_repo
from ..store.db import Store
from .equity_grid import split_cell_id
from .indicators import POSITIONING_LEVEL, positioning_change_leaf

#: A share shown to a reader, not a threshold.
DECIMAL_TO_PERCENT: Final = 100.0

#: The leaves every ranked row carries beside its value: chosen, not every leaf (a row with all
#: of them is the raw table again). Each answers a question the ranked value cannot.
TRAJECTORY_LEAVES: Final = (
    # Direction at two horizons, local and base: a strong month inside a flat quarter is a move
    # still building, a weak month inside a strong quarter one already unwinding.
    "return_21d_local",
    "return_21d_base",
    "return_63d_local",
    "return_63d_base",
    # Whether volatility is expanding or collapsing; negative is a term structure still falling.
    "rv_term_slope",
    # Where the level sits in its own range, from both ends.
    "distance_from_52w_high",
    "distance_from_52w_low",
    # What the cell lived through, wherever in the window.
    "max_drawdown_long_run",
    # The crowd's level, without which its change is unreadable.
    POSITIONING_LEVEL,
)


class Dimension(StrEnum):
    MOST_EXTENDED = "most_extended"
    MOST_COMPRESSED = "most_compressed"
    WIDEST_IV_HV = "widest_iv_hv"
    LARGEST_CORRELATION_SHIFT = "largest_correlation_shift"


@dataclass(frozen=True, slots=True)
class TrajectoryLeaf:
    """One leaf carried onto a ranked row, in the state it was measured in."""

    value: float
    status: MeasurementStatus
    window: str | None
    units: str | None


@dataclass(frozen=True, slots=True)
class RankingRow:
    dimension: Dimension
    rank: int
    market: str
    sector: str
    leaf: str
    value: float
    window: str | None
    status: MeasurementStatus
    market_data_status: MarketDataStatus | None
    detail: str
    reachable: int
    ranked_over: int
    conditions: Mapping[str, bool | None] = field(default_factory=dict)
    trajectory: Mapping[str, TrajectoryLeaf] = field(default_factory=dict)

    @property
    def meets_full_definition(self) -> bool | None:
        """True only when every stated condition held; ``None`` if any is unmeasured."""
        if not self.conditions:
            return None
        values = list(self.conditions.values())
        if any(value is None for value in values):
            return None
        return all(value is True for value in values)


@dataclass(frozen=True, slots=True)
class _ViewSpec:
    dimension: Dimension
    matches: Callable[[str], bool]
    descending: bool
    by_magnitude: bool
    detail: str


def _measured(row: Measurement) -> bool:
    return row.value is not None and row.status in (MeasurementStatus.OK, MeasurementStatus.STALE)


class Rankings:
    """The four ranked views over one built session of the equity grid."""

    def __init__(self, settings: ObliskConfig) -> None:
        self._top_n = settings.grid.ranking_top_n
        self._tenor = settings.collectors.vol.primary_tenor_days
        # "Low" and "high" mean one thing across the build: the screen's quartile boundaries.
        self._low = settings.screen.cheap_iv_percentile_max
        self._high = settings.screen.extended_iv_percentile_min
        self._min_cells = settings.grid.min_cells_for_ranked_view
        self._trajectory_leaves = (
            *TRAJECTORY_LEAVES,
            positioning_change_leaf(settings.sources.positioning.velocity_weeks),
        )
        compressed = f"parkinson_vol_{self._tenor}d_percentile"
        self._views = (
            _ViewSpec(
                Dimension.MOST_EXTENDED,
                lambda leaf: leaf == "distance_from_52w_high",
                descending=True,
                by_magnitude=False,
                detail="nearest its 52-week high; not a short signal on its own",
            ),
            _ViewSpec(
                Dimension.MOST_COMPRESSED,
                lambda leaf: leaf == compressed,
                descending=False,
                by_magnitude=False,
                detail=(
                    "lowest Parkinson realised-vol percentile; compression is the setup. Ranked "
                    "on the range estimator, the least noisy figure the bars support; the "
                    "close-to-close percentile travels on the row as realised_vol_compressed"
                ),
            ),
            _ViewSpec(
                Dimension.WIDEST_IV_HV,
                lambda leaf: leaf == "iv_minus_hv",
                descending=True,
                by_magnitude=True,
                detail=(
                    "largest implied-minus-realised gap in either direction: negative is a cheap "
                    "option, positive an expensive one"
                ),
            ),
            _ViewSpec(
                Dimension.LARGEST_CORRELATION_SHIFT,
                lambda leaf: leaf.startswith("correlation_shift_"),
                descending=True,
                by_magnitude=True,
                detail=(
                    "standard deviations from the pair's own norm on non-overlapping readings; a "
                    "market decoupling from its usual driver is a regime signal"
                ),
            ),
        )

    def build(self, store: Store, *, as_of: date) -> list[RankingRow]:
        """Every view over the newest equity-grid session on or before ``as_of``."""
        session = measurements_repo.measured_session(store, EntityKind.EQUITY_CELL, as_of=as_of)
        if session is None:
            return []
        rows = measurements_repo.session_measurements(store, EntityKind.EQUITY_CELL, as_of=session)
        return self.rank(rows)

    def rank(self, rows: Sequence[Measurement]) -> list[RankingRow]:
        cells: dict[str, dict[str, Measurement]] = {}
        for row in rows:
            cells.setdefault(row.entity_id, {})[row.leaf] = row
        board = len(cells)
        out: list[RankingRow] = []
        for view in self._views:
            out += self._view(view, rows, cells, board)
        return out

    def _view(
        self,
        view: _ViewSpec,
        rows: Sequence[Measurement],
        cells: Mapping[str, Mapping[str, Measurement]],
        board: int,
    ) -> list[RankingRow]:
        candidates = [row for row in rows if view.matches(row.leaf)]
        reachable = len(
            {
                row.entity_id
                for row in candidates
                if row.status is not MeasurementStatus.INAPPLICABLE
            }
        )
        ranked = [row for row in candidates if _measured(row)]
        ranked_over = len({row.entity_id for row in ranked})
        if not ranked:
            return []

        def key(row: Measurement) -> float:
            value = row.value if row.value is not None else 0.0
            return abs(value) if view.by_magnitude else value

        ordered = sorted(ranked, key=key, reverse=view.descending)[: self._top_n]
        detail = self._detail(
            view.detail,
            reachable,
            ranked_over,
            board,
            [split_cell_id(row.entity_id)[0] for row in ranked],
        )
        out: list[RankingRow] = []
        for index, row in enumerate(ordered, start=1):
            market, sector = split_cell_id(row.entity_id)
            cell = cells.get(row.entity_id, {})
            value = row.value if row.value is not None else 0.0
            out.append(
                RankingRow(
                    dimension=view.dimension,
                    rank=index,
                    market=market,
                    sector=sector,
                    leaf=row.leaf,
                    value=value,
                    window=row.window,
                    status=row.status,
                    market_data_status=row.market_data_status,
                    detail=detail,
                    reachable=reachable,
                    ranked_over=ranked_over,
                    conditions=self._conditions(view.dimension, cell),
                    trajectory=self._trajectory(cell, ranked_on=row.leaf),
                )
            )
        return out

    def _detail(
        self, detail: str, reachable: int, ranked_over: int, board: int, markets: Sequence[str]
    ) -> str:
        """The view's description with the size and shape of what it ranked, always: a seven-
        percent, single-market slice printed like a whole-board view is the lens this project
        exists to avoid, and no count threshold catches it."""
        # Called only with a ranked row, so the board and the markets are never empty.
        share = round(DECIMAL_TO_PERCENT * reachable / board)
        parts = [
            detail,
            f"REACH: ranked over {ranked_over} of {reachable} reachable cell(s), {share}% of the "
            f"{board}-cell board",
        ]
        unique = sorted(set(markets))
        if len(unique) == 1:
            parts.append(
                f"EVERY ranked cell is from ONE market ({unique[0]}); this is not a view of the "
                "board and must not be read as one"
            )
        else:
            parts.append(f"markets represented: {', '.join(unique)}")
        if ranked_over < self._min_cells:
            parts.append(
                f"INSUFFICIENT REACH: below the grid.min_cells_for_ranked_view floor of "
                f"{self._min_cells}; an ordering this short is a shortlist, not a ranking"
            )
        return ". ".join(parts)

    def _trajectory(
        self, cell: Mapping[str, Measurement], *, ranked_on: str
    ) -> dict[str, TrajectoryLeaf]:
        """The declared trajectory leaves the cell holds, measured; an unmeasured one is absent
        (zero is a reading), and the ranked leaf is not repeated."""
        out: dict[str, TrajectoryLeaf] = {}
        for name in self._trajectory_leaves:
            row = cell.get(name)
            if name == ranked_on or row is None or row.value is None or not _measured(row):
                continue
            out[name] = TrajectoryLeaf(row.value, row.status, row.window, row.units)
        return out

    def _conditions(
        self, dimension: Dimension, cell: Mapping[str, Measurement]
    ) -> dict[str, bool | None]:
        """The rest of SKILL.md's definition for the view, measured or ``None``. SKILL.md's
        "parabolic trailing return" has no threshold in config, so it is not asserted."""

        def value(name: str) -> float | None:
            row = cell.get(name)
            return None if row is None or not _measured(row) else row.value

        def at_or_below(found: float | None, bound: float) -> bool | None:
            return None if found is None else found <= bound

        def at_or_above(found: float | None, bound: float) -> bool | None:
            return None if found is None else found >= bound

        realised = value(f"realised_vol_{self._tenor}d_percentile")
        parkinson = value(f"parkinson_vol_{self._tenor}d_percentile")
        implied = value("iv_percentile")
        from_high = value("distance_from_52w_high")
        from_low = value("distance_from_52w_low")
        long_run = value("distance_from_long_run_high")
        if dimension is Dimension.MOST_COMPRESSED:
            return {
                "parkinson_vol_compressed": at_or_below(parkinson, self._low),
                "realised_vol_compressed": at_or_below(realised, self._low),
                "iv_percentile_low": at_or_below(implied, self._low),
                # Distances below a high are negative fractions.
                "well_below_long_run_high": None if long_run is None else long_run <= -self._low,
                "near_the_52w_low": at_or_below(from_low, self._low),
            }
        if dimension is Dimension.MOST_EXTENDED:
            return {
                "realised_vol_high": at_or_above(realised, self._high),
                "iv_percentile_high": at_or_above(implied, self._high),
                "near_the_52w_high": None
                if from_high is None
                else from_high >= -(1.0 - self._high),
            }
        return {}
