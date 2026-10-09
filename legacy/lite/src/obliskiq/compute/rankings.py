"""The ranked views, and the cross-market correlation matrix.

**The session reads rankings, not sixteen hundred raw cells.** That is the whole
point of computing the grid by script: a table nobody reads is worse than no
table, because it looks like coverage.

Four views, each ranked on ONE named indicator so the ordering is auditable
rather than a composite score nobody can reproduce:

``most_extended``               ``distance_from_52w_high``
``most_compressed``             ``parkinson_vol_<tenor>d_percentile``
``widest_iv_hv``                ``iv_minus_hv``
``largest_correlation_shift``   ``correlation_shift_<window>d``

``most_compressed`` orders on the PARKINSON percentile rather than the
close-to-close one. Parkinson reads the intraday range that ``bars`` has carried
since migration 001 and is far less noisy on the same sample, which matters most
exactly where the sample is short. What it is NOT is a sharper reading of the
same number: it sees nothing of the overnight gap, so its level sits materially
below the close-to-close figure on any market that closes at night. That is why
the view orders on the PERCENTILE — a rank of Parkinson against Parkinson's own
history, where a bias common to the whole series cancels — and never on the
level, and why ``iv_minus_hv`` and the option screen keep the close-to-close
figure they have always used. ``docs/DECISIONS.md`` carries the measurement.

The close-to-close percentile is untouched in the store and travels on every
``most_compressed`` row as the ``realised_vol_compressed`` condition, so the two
estimators can be read against each other without leaving the table.

``widest_iv_hv`` ranks on ABSOLUTE magnitude and reports the signed value,
because both tails matter: implied far below realised is a cheap option, implied
far above it is an expensive one, and a view that surfaced only one would hide
half the dislocation screen.

Every ranked row also carries a **named, bounded set of trajectory leaves** from
the same cell and the same ``as_of``: a percentile states a LEVEL and says
nothing about whether that level is still building or already unwinding, and the
answer was in the grid all along, one ``get_grid_cell`` call per row away. They
are SERVED, never recomputed — ``_cells_for`` already read every measured leaf of
the ranked cells in one query for the conditions map — and each travels with its
own ``status`` and ``window``, because a stale return beside a fresh percentile
with nothing to tell them apart is the failure migration 012 was written to end.
A leaf the store could not measure is ABSENT from the map. Never zero: zero is a
reading.

The correlation matrix compares every pair against **its own** norm rather than
a shared threshold. Two markets that always sit at 0.9 and two that never
correlate both deviate meaningfully from their own history, and a single global
number sees only the first.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from itertools import combinations
from typing import Any

from ..config import Config
from ..logging_setup import get_logger
from ..store.db import Store
from ..util.timeutil import iso_date, utc_stamp
from .frames import bar_series, contract_id_for_symbol, macro_series
from .indicators import (
    STATUS_INAPPLICABLE,
    STATUS_NA,
    STATUS_OK,
    STATUS_STALE,
    positioning_change_indicator,
)
from .stats import (
    TRANSFORM_LEVEL_CHANGE,
    TRANSFORM_LOG_RETURN,
    correlation,
    norm_deviation,
    paired_changes,
    rolling_correlations,
)

log = get_logger("compute.rankings")

#: The asset classes the correlation matrix admits. Equity comes off daily bars;
#: fx and rate come off the macro store, point-in-time.
ASSET_EQUITY = "equity"
ASSET_FX = "fx"
ASSET_RATE = "rate"

#: WHICH PAIRS ARE COMPUTED, and this is architecture rather than a threshold —
#: a module constant for the same reason ``DAILY_SEQUENCE`` is one. A config key
#: able to reorder or widen it would make the shape of the board switchable by a
#: setting, which is the failure the Phase 3 sweep deleted nineteen keys for.
#:
#: Equity x equity is the matrix as it has always been. Equity x fx and
#: equity x rates are what this adds: equity against rates flipping sign is one
#: of the cleanest regime markers there is. FX x FX and rate x rate are
#: deliberately NOT here — eleven ECB rates against each other is the dollar
#: index measured eleven ways, and every euro-area market shares one policy
#: rate, so rate x rate would mostly correlate ECB_DFR with itself.
CORRELATED_CLASS_PAIRS = (
    (ASSET_EQUITY, ASSET_EQUITY),
    (ASSET_EQUITY, ASSET_FX),
    (ASSET_EQUITY, ASSET_RATE),
)

#: The market-spec keys each non-equity class reads, in ``universe.yaml``. A
#: market declaring one as null is a declared coverage gap and contributes no leg
#: — which is the same treatment indicators 11-13 give the same fields.
_RATE_SERIES_KEYS = ("policy_rate_series", "long_rate_series")
_FX_SERIES_KEY = "fx_series"

#: The four ranked dimensions.
DIMENSION_MOST_EXTENDED = "most_extended"
DIMENSION_MOST_COMPRESSED = "most_compressed"
DIMENSION_WIDEST_IV_HV = "widest_iv_hv"
DIMENSION_LARGEST_CORRELATION_SHIFT = "largest_correlation_shift"

#: A share reported to a reader, not a threshold anything is compared against.
#: The same constant the other three modules that render a percentage carry.
DECIMAL_TO_PERCENT = 100.0

DIMENSIONS = (
    DIMENSION_MOST_EXTENDED,
    DIMENSION_MOST_COMPRESSED,
    DIMENSION_WIDEST_IV_HV,
    DIMENSION_LARGEST_CORRELATION_SHIFT,
)

# DIMENSION_INDICATOR_PREFIX was here, naming the leaf each dimension ranks on,
# and nothing read it — so it could disagree with `build` indefinitely, and did.
# Deleted rather than corrected; see docs/DECISIONS.md. The docstring above is
# the reader's index and `build` below is the authority.


@dataclass(frozen=True)
class TrajectoryLeaf:
    """One grid leaf carried onto a ranked row, in the state it was measured in.

    ``status`` and ``window`` are not decoration. The leaves come from four
    different stores on four cadences, so a row can legitimately hold a fresh
    percentile beside a stale return, and the whole point of migration 012 was
    that a table which cannot say which is which is a table that reads as
    healthier than the store is. ``units`` travels for the same reason the grid
    carries it: a ratio and a decimal fraction print identically and mean
    different things.
    """

    value: float
    status: str
    window: str | None
    units: str | None


#: The trajectory leaves every ranked row carries beside its own value.
#:
#: A CHOSEN set, not every leaf. A market x sector cell holds forty-odd of them —
#: the count comes out of config and moves with it — and a row carrying all of
#: them is the raw table again, which
#: is the thing the ranked views exist to avoid. Each one below earns its space
#: by answering a question the ranked value cannot, and the positioning change
#: windows are appended from ``commodities.cot.change_windows_weeks`` in
#: ``Rankings.__init__`` so the set stays in step with what family 15 emits.
TRAJECTORY_LEAVES: tuple[str, ...] = (
    # Direction, at two horizons, because one horizon cannot separate the two
    # cases the whole change exists for: a strong month inside a flat quarter is
    # a move still building, and a weak month inside a strong quarter is one
    # already unwinding. Local AND base at both, because SKILL.md requires every
    # market's return in both currencies wherever it is reported — a market up
    # sixty percent locally can be down in the account's currency, and a
    # trajectory read off the wrong one is a trajectory the account never had.
    "return_21d_local",
    "return_21d_base",
    "return_63d_local",
    "return_63d_base",
    # Whether volatility is expanding or collapsing — the distinction the
    # compression screen turns on and the one thing a single percentile cannot
    # show. Short tenor over long: negative is a term structure still falling.
    "rv_term_slope",
    # Where the level sits in its own range, from both ends. A compressed cell
    # pinned to its 52-week low and one sitting at the high are opposite setups,
    # and the ranked value is identical in both.
    "distance_from_52w_high",
    "distance_from_52w_low",
    # What the cell lived THROUGH, wherever in the window it happened. A series
    # fully recovered from a forty percent collapse has no distance from its high
    # and is not the same risk as one that never fell; that is the whole reason
    # family 4 is separate from family 2.
    "max_drawdown_long_run",
    # The crowd's LEVEL, carried because the change below is unreadable without
    # it: two points of open interest off a 95th-percentile book and the same two
    # points off a median one are different facts. Absent on every market that
    # declares no COT series, which is most of them — and absent is how it
    # travels, never as a zero.
    "positioning_percentile",
)


@dataclass(frozen=True)
class RankingRow:
    """One row of one ranked view."""

    dimension: str
    rank: int
    market: str
    sector: str
    indicator: str
    value: float | None
    window: str | None
    detail: str | None = None
    #: ``ok`` or ``stale``, as the grid cell was stamped. Ranked views admit both
    #: — a stale reading is still the best available — and carried neither, so
    #: the tables a session actually reads ordered stale cells beside fresh ones
    #: with nothing to tell them apart. SKILL.md wants the label in EVERY table.
    status: str | None = None
    #: The quote state the cell's inputs were captured in, unchanged from the
    #: cell: FROZEN, DELAYED, REALTIME, or None where the route reported nothing.
    market_data_status: str | None = None
    #: The REST of SKILL.md's definition, for the views that have one. Each view
    #: still ORDERS on one named indicator, so the ordering stays reproducible —
    #: a composite score nobody can check is what that design refuses. These say
    #: whether the other conditions held, so ``most_compressed`` cannot be read
    #: as "coiled" when only compression was measured. ``None`` for a condition
    #: the store could not test, which is never the same as False.
    conditions: dict[str, bool | None] = field(default_factory=dict)
    #: The trajectory leaves of the SAME cell at the SAME ``as_of``, from
    #: ``TRAJECTORY_LEAVES``. A ranked value is a level; these say which way it is
    #: moving, and they were already computed — this serves them rather than
    #: recomputing anything. A leaf the store could not measure is absent from the
    #: map, never present as a zero, and the leaf this row already RANKS on is not
    #: repeated here: one number under two names invites a reader to look for a
    #: difference between them.
    trajectory: dict[str, TrajectoryLeaf] = field(default_factory=dict)
    #: Market x sector cells where this view's indicator is not structurally
    #: inapplicable — what the view COULD have ranked. The option views reached
    #: two cells in the whole grid before the US sector funds were mapped, while
    #: printing exactly like the views that reach a hundred.
    reachable: int | None = None
    #: Cells that actually held a usable value, BEFORE the top-N truncation. So
    #: ranked_over <= reachable, and a view showing ten rows out of ten ranked
    #: over eleven cells is a different claim from one showing ten out of a
    #: hundred.
    ranked_over: int | None = None

    @property
    def is_thin(self) -> bool | None:
        """Did this view rank fewer cells than it could reach? ``None`` if unrecorded."""
        if self.reachable is None or self.ranked_over is None:
            return None
        return self.ranked_over < self.reachable

    @property
    def meets_full_definition(self) -> bool | None:
        """True only when every stated condition held; ``None`` if any is unmeasured."""
        if not self.conditions:
            return None
        values = list(self.conditions.values())
        if any(value is None for value in values):
            return None
        return all(values)


#: Equity bars are daily by construction, so an equity leg declares that rather
#: than leaving the frequency unknown and skipping the mixing check.
_FREQUENCY_DAILY = "daily"


@dataclass(frozen=True)
class _LegSeries:
    """One leg's values by ISO date, with what the store says about them."""

    values: dict[str, float]
    frequencies: set[str]
    units: set[str]


@dataclass(frozen=True)
class CorrelationLeg:
    """One side of a correlated pair: what it is, whose it is, where it comes from.

    A MARKET no longer identifies a leg. `germany` has an equity index, a policy
    rate and a long rate, and correlating germany's equities against germany's
    rates is a pair between two legs of the same market — which is the regime
    marker this exists for.
    """

    asset_class: str
    #: The macro series id for `fx` and `rate`. For `equity` it is the MARKET
    #: NAME: a market is measured on exactly one index contract, so the market
    #: names that series uniquely, and using it keeps every pre-019 row's
    #: identity intact.
    series: str
    market: str

    @property
    def transform(self) -> str:
        """Log return for a price, level change for a rate. A function of the class.

        Not a stored column for the reason ``docs/COMPUTE.md`` gives for the
        realised-vol estimators: a derived fact with a column of its own is free
        to drift out of step with the value beside it.
        """
        return TRANSFORM_LEVEL_CHANGE if self.asset_class == ASSET_RATE else TRANSFORM_LOG_RETURN

    @property
    def key(self) -> tuple[str, str]:
        """What orders a pair canonically, so (A,B) and (B,A) are never both written."""
        return (self.asset_class, self.series)

    def describe(self) -> str:
        if self.asset_class == ASSET_EQUITY:
            return f"{self.market} equities"
        return f"{self.market} {self.asset_class} ({self.series})"


@dataclass(frozen=True)
class CorrelationCell:
    """One pair at one window, against the pair's own norm."""

    left: CorrelationLeg
    right: CorrelationLeg
    window_days: int
    correlation: float | None
    norm_mean: float | None
    norm_sd: float | None
    z_score: float | None
    flagged: bool
    status: str
    reason: str | None = None
    #: Aligned observations the pair HAS — the calendar-date intersection of the
    #: two series, after differencing. The correlation itself reads the newest
    #: ``window_days`` of them and the norm reads all of them, so this is the
    #: number that says whether either was possible; ``window_days`` already says
    #: how wide the correlation is.
    n_obs: int | None = None
    #: Non-overlapping readings behind the norm.
    norm_independent: int | None = None


class Rankings:
    """Reads the grid and emits the four views the session actually looks at."""

    def __init__(self, store: Store, config: Config) -> None:
        self._store = store
        self._config = config
        self._top_n = config.require_int("data.grid_ranking_top_n")
        self._reference_tenor = config.require_int("data.strike_bounds_rv_tenor_days")
        # The boundaries the REST of SKILL.md's definitions turn on. The same two
        # the screener buckets on, deliberately: "low" and "high" have to mean
        # one thing across the build, or the grid and the screen describe the
        # same instrument in two vocabularies.
        self._low = config.require_float("screen.cheap_iv_percentile_max")
        self._high = config.require_float("screen.extended_iv_percentile_min")
        # Below this many cells a view is LABELLED, never suppressed. Suppressing
        # `widest_iv_hv` would delete the only implied-vol signal the store holds
        # to avoid admitting it is thin; the label costs nothing and says exactly
        # what the reader is looking at.
        self._min_cells = config.require_int("grid.min_cells_for_ranked_view")
        # The declared trajectory set, plus one change leaf per configured COT
        # window. Named through indicators.positioning_change_indicator so the
        # family that emits the leaf and the view that carries it cannot spell it
        # differently — the failure DIMENSION_INDICATOR_PREFIX was deleted for.
        self._trajectory_leaves = TRAJECTORY_LEAVES + tuple(
            positioning_change_indicator(int(weeks))
            for weeks in config.require_list("commodities.cot.change_windows_weeks")
        )

    def build(self, *, as_of: date) -> list[RankingRow]:
        rows: list[RankingRow] = []
        rows.extend(
            self._rank(
                as_of,
                DIMENSION_MOST_EXTENDED,
                indicator="distance_from_52w_high",
                descending=True,
                detail="nearest its 52-week high; not a short signal on its own",
            )
        )
        rows.extend(
            self._rank(
                as_of,
                DIMENSION_MOST_COMPRESSED,
                indicator=f"parkinson_vol_{self._reference_tenor}d_percentile",
                descending=False,
                detail=(
                    "lowest Parkinson realised-vol percentile; compression is the setup. "
                    "Ranked on the range-based estimator, which is the least noisy figure "
                    "the stored bars support; the close-to-close percentile is unchanged in "
                    "the store and travels on the row as realised_vol_compressed"
                ),
            )
        )
        rows.extend(
            self._rank(
                as_of,
                DIMENSION_WIDEST_IV_HV,
                indicator="iv_minus_hv",
                descending=True,
                by_magnitude=True,
                detail=(
                    "largest implied-minus-realised gap in either direction: negative is a "
                    "cheap option, positive an expensive one"
                ),
            )
        )
        rows.extend(self._rank_correlation_shift(as_of))
        return rows

    def write(self, rows: Sequence[RankingRow], *, as_of: date) -> int:
        if not rows:
            return 0
        stamp = utc_stamp()
        with self._store.transaction() as connection:
            # A rebuild must not leave yesterday's tail behind a shorter list today.
            connection.execute("DELETE FROM grid_rankings WHERE as_of = ?", (iso_date(as_of),))
            connection.executemany(
                "INSERT INTO grid_rankings (as_of, dimension, rank, market, sector, "
                "indicator, value, window, detail, status, market_data_status, conditions, "
                "reachable, ranked_over, trajectory, computed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        iso_date(as_of),
                        row.dimension,
                        row.rank,
                        row.market,
                        row.sector,
                        row.indicator,
                        row.value,
                        row.window,
                        row.detail,
                        row.status,
                        row.market_data_status,
                        json.dumps(row.conditions) if row.conditions else None,
                        row.reachable,
                        row.ranked_over,
                        _encode_trajectory(row.trajectory),
                        stamp,
                    )
                    for row in rows
                ],
            )
        return len(rows)

    def read(self, *, as_of: date, dimension: str) -> list[dict[str, Any]]:
        rows = self._store.execute(
            "SELECT * FROM grid_rankings WHERE as_of = ? AND dimension = ? ORDER BY rank",
            (iso_date(as_of), dimension),
        ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            record = dict(row)
            # Stored as JSON so the row stays one row. Decoded here so every
            # reader gets the conditions as data rather than as a string it has
            # to remember to parse.
            record["conditions"] = _decode_object(record.get("conditions"), "conditions")
            record["trajectory"] = _decode_object(record.get("trajectory"), "trajectory")
            out.append(record)
        return out

    # ── internals ──

    def _reach(self, as_of: date, *, indicator: str, like: bool = False) -> tuple[int, int]:
        """(cells this indicator can reach, cells that held a usable value).

        Counted in CELLS rather than rows because the correlation shift emits one
        leaf per window: three rows for one market x sector, which as a row count
        would treble a view's apparent breadth.

        Reachable EXCLUDES only ``inapplicable``. A cell that is `n/a` because
        nothing has been collected yet is reachable and unranked, which is the
        difference the two numbers exist to show — collection owes it, and no
        amount of collecting will produce the other kind.
        """
        clause = "indicator LIKE ?" if like else "indicator = ?"
        reachable = self._store.execute(
            f"SELECT COUNT(DISTINCT market || '/' || sector) FROM grid "
            f"WHERE as_of = ? AND {clause} AND status != ?",
            (iso_date(as_of), indicator, STATUS_INAPPLICABLE),
        ).fetchone()
        ranked = self._store.execute(
            f"SELECT COUNT(DISTINCT market || '/' || sector) FROM grid "
            f"WHERE as_of = ? AND {clause} AND status IN (?, ?) AND value IS NOT NULL",
            (iso_date(as_of), indicator, STATUS_OK, STATUS_STALE),
        ).fetchone()
        return (int(reachable[0]) if reachable else 0, int(ranked[0]) if ranked else 0)

    def _detail(
        self,
        detail: str,
        reachable: int,
        ranked_over: int,
        *,
        board: int = 0,
        markets: Sequence[str] = (),
    ) -> str:
        """The view's own description, with the size and shape of what it ranked.

        The numbers travel on the row either way. This puts them where a reader
        who is looking at the table rather than at its schema will see them.

        **The share and the market list are stated ALWAYS, not past a threshold.**
        The four views print identically whatever they ranked, and the absolute
        floor below is not what makes one misleading: measured 2026-09-11,
        `widest_iv_hv` ranked 11 of 11 reachable cells — above the floor, so
        silent — and every one of those eleven was `us`. A seven-percent, single
        market slice of the board presented in the same shape as a whole-board
        one is the US lens this project exists to avoid, and no count
        threshold catches it because the count was never the problem. Disclosing
        the denominator costs nothing and needs no new number to be chosen.
        """
        parts = [detail]
        if board > 0:
            share = round(DECIMAL_TO_PERCENT * reachable / board)
            parts.append(
                f"REACH: ranked over {ranked_over} of {reachable} reachable cell(s), "
                f"{share}% of the {board}-cell board"
            )
        if markets:
            unique = sorted(set(markets))
            if len(unique) == 1:
                parts.append(
                    f"EVERY ranked cell is from ONE market ({unique[0]}). This view is "
                    "not a view of the board and must not be read as one"
                )
            else:
                parts.append(f"markets represented: {', '.join(unique)}")
        if ranked_over < self._min_cells:
            parts.append(
                f"INSUFFICIENT REACH: below the grid.min_cells_for_ranked_view floor of "
                f"{self._min_cells}. An ordering this short is a shortlist, not a "
                "ranking, and must not be read as one"
            )
        return ". ".join(parts)

    def _rank(
        self,
        as_of: date,
        dimension: str,
        *,
        indicator: str,
        descending: bool,
        by_magnitude: bool = False,
        detail: str,
    ) -> list[RankingRow]:
        rows = self._store.execute(
            "SELECT market, sector, indicator, value, window, status, market_data_status "
            "FROM grid WHERE as_of = ? AND indicator = ? AND status IN (?, ?) "
            "AND value IS NOT NULL",
            (iso_date(as_of), indicator, STATUS_OK, STATUS_STALE),
        ).fetchall()
        if not rows:
            return []

        def key(row: Any) -> float:
            value = float(row["value"])
            return abs(value) if by_magnitude else value

        ordered = sorted(rows, key=key, reverse=descending)[: self._top_n]
        cells = self._cells_for(as_of, [(str(r["market"]), str(r["sector"])) for r in ordered])
        reachable, ranked_over = self._reach(as_of, indicator=indicator)
        described = self._detail(
            detail,
            reachable,
            ranked_over,
            board=self._board_size(as_of),
            markets=[str(r["market"]) for r in rows],
        )
        return [
            RankingRow(
                dimension=dimension,
                rank=index + 1,
                market=str(row["market"]),
                sector=str(row["sector"]),
                indicator=str(row["indicator"]),
                value=float(row["value"]),
                window=row["window"],
                detail=described,
                status=row["status"],
                market_data_status=row["market_data_status"],
                conditions=self._conditions(
                    dimension, cells.get((str(row["market"]), str(row["sector"])), {})
                ),
                trajectory=self._trajectory(
                    cells.get((str(row["market"]), str(row["sector"])), {}),
                    ranked_on=str(row["indicator"]),
                ),
                reachable=reachable,
                ranked_over=ranked_over,
            )
            for index, row in enumerate(ordered)
        ]

    def _board_size(self, as_of: date) -> int:
        """How many market x sector cells this build wrote, measured or not.

        The denominator a share is meaningful against. Read from the grid rather
        than from the declared axes so it describes the build in hand: a view is
        a share of what was actually written, not of what was intended.
        """
        row = self._store.execute(
            "SELECT COUNT(DISTINCT market || '/' || sector) FROM grid WHERE as_of = ?",
            (iso_date(as_of),),
        ).fetchone()
        return int(row[0]) if row and row[0] else 0

    def _cells_for(
        self, as_of: date, keys: Sequence[tuple[str, str]]
    ) -> dict[tuple[str, str], dict[str, TrajectoryLeaf]]:
        """Every measured leaf of the given cells, by market/sector.

        One query for the whole page rather than one per row: the ranked views
        are read together, and the conditions and the trajectory come from the
        same build. Only ``ok`` and ``stale`` cells with a value are read, so a
        leaf the store could not measure is simply absent — which is what lets
        both callers report absence as absence instead of as a number.
        """
        if not keys:
            return {}
        wanted = set(keys)
        rows = self._store.execute(
            "SELECT market, sector, indicator, value, status, window, units FROM grid "
            "WHERE as_of = ? AND status IN (?, ?) AND value IS NOT NULL",
            (iso_date(as_of), STATUS_OK, STATUS_STALE),
        ).fetchall()
        out: dict[tuple[str, str], dict[str, TrajectoryLeaf]] = {}
        for row in rows:
            key = (str(row["market"]), str(row["sector"]))
            if key in wanted:
                out.setdefault(key, {})[str(row["indicator"])] = TrajectoryLeaf(
                    value=float(row["value"]),
                    status=str(row["status"]),
                    window=row["window"],
                    units=row["units"],
                )
        return out

    def _trajectory(
        self, cell: dict[str, TrajectoryLeaf], *, ranked_on: str
    ) -> dict[str, TrajectoryLeaf]:
        """The declared trajectory leaves this cell actually holds.

        SERVED, not recomputed: every one of these was written by
        ``obliskiq-compute grid`` for the same cell on the same ``as_of``, and
        the alternative to carrying them here was a ``get_grid_cell`` call per
        ranked row to find out whether a 94th-percentile reading was still
        building or already unwinding.

        A leaf the cell does not hold is left OUT rather than carried as a null
        or a zero. And the leaf the row already ranks on is left out too: it is
        the row's own ``value``, and repeating it under a second name invites a
        reader to look for a difference that cannot exist.
        """
        return {
            name: cell[name]
            for name in self._trajectory_leaves
            if name in cell and name != ranked_on
        }

    def _conditions(
        self, dimension: str, cell: dict[str, TrajectoryLeaf]
    ) -> dict[str, bool | None]:
        """The rest of SKILL.md's definition for this view, measured or ``None``.

        Each view still ORDERS on one named indicator — a composite score nobody
        can reproduce is exactly what that design refuses — so these travel
        beside the ordering instead of inside it. Without them a session reads
        "most compressed" as *coiled*, which is four conditions, when only one
        was tested.

        SKILL.md's *extended* also names a "parabolic trailing return". No
        threshold in config defines parabolic, so it is not asserted here rather
        than being given an invented one; the return leaves are in the cell for
        a reader who wants them.
        """

        def measured(name: str) -> float | None:
            """The leaf's value, or None where the store did not measure it."""
            leaf = cell.get(name)
            return None if leaf is None else leaf.value

        rv_percentile = measured(f"realised_vol_{self._reference_tenor}d_percentile")
        parkinson_percentile = measured(f"parkinson_vol_{self._reference_tenor}d_percentile")
        iv_percentile = measured("iv_percentile")
        from_52w_high = measured("distance_from_52w_high")
        from_52w_low = measured("distance_from_52w_low")
        from_long_run_high = measured("distance_from_long_run_high")

        def at_or_below(value: float | None, bound: float) -> bool | None:
            return None if value is None else value <= bound

        def at_or_above(value: float | None, bound: float) -> bool | None:
            return None if value is None else value >= bound

        if dimension == DIMENSION_MOST_COMPRESSED:
            return {
                # The ordering variable's own condition, and the close-to-close
                # one beside it. Both, because the two estimators can disagree
                # about the same instrument on the same day and a reader is
                # entitled to see that rather than to infer it.
                "parkinson_vol_compressed": at_or_below(parkinson_percentile, self._low),
                "realised_vol_compressed": at_or_below(rv_percentile, self._low),
                "iv_percentile_low": at_or_below(iv_percentile, self._low),
                # Distances below a high are negative fractions, so "well below"
                # is at or beyond the boundary in the negative direction.
                "well_below_long_run_high": (
                    None if from_long_run_high is None else from_long_run_high <= -self._low
                ),
                "near_the_52w_low": at_or_below(from_52w_low, self._low),
            }
        if dimension == DIMENSION_MOST_EXTENDED:
            return {
                "realised_vol_high": at_or_above(rv_percentile, self._high),
                "iv_percentile_high": at_or_above(iv_percentile, self._high),
                "near_the_52w_high": (
                    None if from_52w_high is None else from_52w_high >= -(1.0 - self._high)
                ),
            }
        return {}

    def _rank_correlation_shift(self, as_of: date) -> list[RankingRow]:
        """Across every configured window at once: the largest shift is the signal.

        Correlation CHANGE matters more than level — a market decoupling from its
        usual driver is a regime signal — so the shift cells from all windows are
        ranked together and each row keeps the window it came from.
        """
        rows = self._store.execute(
            "SELECT market, sector, indicator, value, window, status, market_data_status "
            "FROM grid WHERE as_of = ? AND indicator LIKE 'correlation_shift_%' "
            "AND status IN (?, ?) AND value IS NOT NULL",
            (iso_date(as_of), STATUS_OK, STATUS_STALE),
        ).fetchall()
        ordered = sorted(rows, key=lambda row: abs(float(row["value"])), reverse=True)[
            : self._top_n
        ]
        cells = self._cells_for(as_of, [(str(r["market"]), str(r["sector"])) for r in ordered])
        reachable, ranked_over = self._reach(as_of, indicator="correlation_shift_%", like=True)
        detail = self._detail(
            "standard deviations from this pair's own norm, measured on non-overlapping "
            "readings; a market decoupling from its usual driver is a regime signal",
            reachable,
            ranked_over,
            board=self._board_size(as_of),
            markets=[str(r["market"]) for r in rows],
        )
        return [
            RankingRow(
                dimension=DIMENSION_LARGEST_CORRELATION_SHIFT,
                rank=index + 1,
                market=str(row["market"]),
                sector=str(row["sector"]),
                indicator=str(row["indicator"]),
                value=float(row["value"]),
                window=row["window"],
                detail=detail,
                status=row["status"],
                market_data_status=row["market_data_status"],
                # This view names no conditions — SKILL.md defines no "coiled"
                # for a correlation shift — but a decoupling still happens to a
                # market that is moving somewhere, and the trajectory says where.
                trajectory=self._trajectory(
                    cells.get((str(row["market"]), str(row["sector"])), {}),
                    ranked_on=str(row["indicator"]),
                ),
                reachable=reachable,
                ranked_over=ranked_over,
            )
            for index, row in enumerate(ordered)
        ]


class CorrelationMatrix:
    """Every admitted pair, at every configured window, against its own norm.

    Cross-asset since 019. The matrix computed market-to-market equity
    correlations from the day it was written, while FX sat in the store as eleven
    ECB reference rates and rates sat there as a policy rate and a 10-year yield
    per market, and neither entered it — although equity against rates flipping
    sign is one of the cleanest regime markers there is and both series were
    already being collected.

    What the classes cost, and it is paid honestly. A rate series is not a price
    series, so its change is a level difference rather than a log return. Most
    declared rate series are MONTHLY while equity bars are daily, and a
    daily-against-monthly pair is REFUSED rather than resampled. And every pair
    still faces ``grid.correlation_norm_min_independent``, which most cross-asset
    pairs will not clear for a long time. All three produce `n/a` rows with
    counts rather than numbers, which is the answer this build prefers.
    """

    def __init__(self, store: Store, config: Config, markets: dict[str, dict[str, Any]]) -> None:
        self._store = store
        self._config = config
        self._markets = markets
        data = config.section("data")
        self._windows = [int(w) for w in data.require_list("correlation_windows_days")]
        self._weekly_window_years = data.require_int("weekly_window_years")
        self._trading_days_per_year = data.require_float("trading_days_per_year")
        self._calendar_days_per_year = config.require_float("numerics.bsm.day_count_basis")
        self._shift_sd = config.require_float("grid.correlation_shift_sd")
        self._norm_years = config.require_int("grid.correlation_norm_window_years")
        self._norm_sessions = round(self._norm_years * self._trading_days_per_year)
        self._min_observations = config.require_int("grid.min_observations.correlation")
        self._norm_min_independent = config.require_int("grid.correlation_norm_min_independent")

    def build(self, *, as_of: date) -> list[CorrelationCell]:
        legs = self._legs()
        series = {leg.key: self._values_for(leg, as_of=as_of) for leg in legs}
        cells: list[CorrelationCell] = []
        for left, right in self._pairs(legs):
            for window in self._windows:
                cells.append(
                    self._cell(left, right, window, series.get(left.key), series.get(right.key))
                )
        return cells

    def write(self, cells: Sequence[CorrelationCell], *, as_of: date) -> int:
        if not cells:
            return 0
        stamp = utc_stamp()
        with self._store.transaction() as connection:
            connection.executemany(
                """
                INSERT INTO correlation_matrix (
                    as_of, asset_class_a, series_a, asset_class_b, series_b,
                    market_a, market_b, window_days, correlation, n_obs, norm_mean, norm_sd,
                    norm_independent, z_score, flagged, status, reason, computed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (as_of, asset_class_a, series_a, asset_class_b, series_b, window_days)
                DO UPDATE SET
                    market_a = excluded.market_a,
                    market_b = excluded.market_b,
                    correlation = excluded.correlation,
                    n_obs = excluded.n_obs,
                    norm_mean = excluded.norm_mean,
                    norm_sd = excluded.norm_sd,
                    norm_independent = excluded.norm_independent,
                    z_score = excluded.z_score,
                    flagged = excluded.flagged,
                    status = excluded.status,
                    reason = excluded.reason,
                    computed_at = excluded.computed_at
                """,
                [
                    (
                        iso_date(as_of),
                        cell.left.asset_class,
                        cell.left.series,
                        cell.right.asset_class,
                        cell.right.series,
                        cell.left.market,
                        cell.right.market,
                        cell.window_days,
                        cell.correlation,
                        cell.n_obs,
                        cell.norm_mean,
                        cell.norm_sd,
                        cell.norm_independent,
                        cell.z_score,
                        int(cell.flagged),
                        cell.status,
                        cell.reason,
                        stamp,
                    )
                    for cell in cells
                ],
            )
        return len(cells)

    def flagged(self, *, as_of: date) -> list[dict[str, Any]]:
        """Pairs beyond ``grid.correlation_shift_sd`` of their own norm."""
        rows = self._store.execute(
            "SELECT * FROM correlation_matrix WHERE as_of = ? AND flagged = 1 "
            "ORDER BY ABS(z_score) DESC",
            (iso_date(as_of),),
        ).fetchall()
        return [dict(row) for row in rows]

    # ── internals ──

    def _legs(self) -> list[CorrelationLeg]:
        """Every DISTINCT leg the declarations offer, in a stable order.

        A market declaring a series as null is a declared coverage gap and
        contributes no leg — the same treatment indicators 11-13 give the same
        fields. Euro-area markets declare ``fx_series: null`` because the base
        currency has no cross against itself.

        **One series can be claimed by several markets**, and that is the whole
        reason this deduplicates here rather than at the pair. Six euro-area
        markets declare `ECB_DFR` as their policy rate; it is ONE series and one
        leg, and emitting six would write the same measurement six times under
        six different `market_b` values. The first market in sorted order is
        recorded as the owner, and every other claimant is dropped — so which
        market appears beside a shared series is a stated rule rather than
        whatever the iteration happened to reach first. Group on `series_b` to
        find such a pair; `market_b` names one claimant, not all of them.
        """
        legs: dict[tuple[str, str], CorrelationLeg] = {}

        def offer(leg: CorrelationLeg) -> None:
            legs.setdefault(leg.key, leg)

        for market in sorted(self._markets):
            spec = self._markets[market]
            offer(CorrelationLeg(ASSET_EQUITY, market, market))
            fx = spec.get(_FX_SERIES_KEY)
            if fx:
                offer(CorrelationLeg(ASSET_FX, str(fx), market))
            for key in _RATE_SERIES_KEYS:
                series_id = spec.get(key)
                if series_id:
                    offer(CorrelationLeg(ASSET_RATE, str(series_id), market))
        return sorted(legs.values(), key=lambda leg: leg.key)

    def _pairs(self, legs: list[CorrelationLeg]):
        """Unordered pairs whose class combination ``CORRELATED_CLASS_PAIRS`` admits.

        ``legs`` is already distinct and sorted on ``(asset_class, series)``, so
        ``combinations`` yields each pair once and in one direction — the primary
        key would accept both directions and they are the same measurement.
        """
        admitted = {frozenset(pair) for pair in CORRELATED_CLASS_PAIRS}
        for left, right in combinations(legs, 2):
            if frozenset((left.asset_class, right.asset_class)) in admitted:
                yield left, right

    def _values_for(self, leg: CorrelationLeg, *, as_of: date) -> _LegSeries | None:
        """The leg's series by calendar date, or ``None`` with nothing to say.

        Equity comes off the native index's daily bars. FX and rates come off the
        macro store through ``observations_known_at``, which is the ONLY
        point-in-time read: it keeps an observation only once its RELEASE date
        has passed, so a correlation is never built on a print nobody had.
        """
        since = as_of - timedelta(
            days=round(self._weekly_window_years * self._calendar_days_per_year)
        )
        if leg.asset_class == ASSET_EQUITY:
            symbol = str(self._markets.get(leg.market, {}).get("native_index") or "")
            contract_id = contract_id_for_symbol(self._store, symbol) if symbol else None
            if contract_id is None:
                return None
            bars = bar_series(self._store, contract_id, since=since)
            values = {
                stamp[:_DATE_LENGTH]: close
                for stamp, close in zip(bars.ts, bars.close, strict=True)
            }
            # A contract that resolves but holds no bars is ABSENT, not a series
            # of length zero. Returning an empty _LegSeries would reach the
            # overlap floor and report "0 overlapping observation(s)", which
            # blames the pair for a gap that belongs to one side of it.
            if not values:
                return None
            return _LegSeries(values=values, frequencies={_FREQUENCY_DAILY}, units=set())

        series = macro_series(self._store, leg.series, since=since)
        observations = series.observations_known_at(as_of)
        if not observations:
            return None
        # Filtered on RELEASE date, aligned on REFERENCE date. The release date
        # decides what was knowable; the reference date is when the value
        # applied, and is what a bar on the same day can be paired with. Keying
        # the intersection on release dates instead would pair a bar with almost
        # nothing — a monthly series has twelve release dates a year.
        return _LegSeries(
            values={o.reference_date.isoformat(): o.value for o in observations},
            frequencies=series.frequencies_known_at(as_of),
            units=series.units_known_at(as_of),
        )

    def _cell(
        self,
        left: CorrelationLeg,
        right: CorrelationLeg,
        window: int,
        left_series: _LegSeries | None,
        right_series: _LegSeries | None,
    ) -> CorrelationCell:
        def absent(reason: str, *, n_obs: int | None = None) -> CorrelationCell:
            return CorrelationCell(
                left=left,
                right=right,
                window_days=window,
                correlation=None,
                norm_mean=None,
                norm_sd=None,
                z_score=None,
                flagged=False,
                status=STATUS_NA,
                reason=reason,
                n_obs=n_obs,
            )

        if left_series is None or right_series is None:
            missing = left if left_series is None else right
            detail = (
                "has no bars for its native index; a US-listed wrapper is not a substitute"
                if missing.asset_class == ASSET_EQUITY
                else (
                    f"has no release-dated observations of {missing.series} on or before this "
                    "session; a value with no publication date is invisible point-in-time"
                )
            )
            return absent(f"{missing.describe()} {detail}, so the pair cannot be correlated.")

        # REFUSED, never resampled, and the refusal FAILS CLOSED. Correlating a
        # daily series against a monthly one compares twelve readings a year with
        # two hundred and fifty, and any alignment making the lengths match has
        # resampled one of them — MacroPull._sane refuses the same thing one
        # layer up.
        #
        # `macro.frequency` is nullable, so "no declared cadence" is a real state
        # and it is NOT treated as agreement. Unioning an empty set into a daily
        # one would leave a single element and let an undeclared series through
        # as daily, which is the fail-open version of this guard and worth more
        # than the pairs it costs.
        for leg, series in ((left, left_series), (right, right_series)):
            if not series.frequencies:
                return absent(
                    f"{leg.describe()} declares no frequency on any observation in this "
                    "window, so there is nothing to check a daily equity series against. "
                    "An undeclared cadence is refused rather than assumed to match."
                )
            if len(series.frequencies) > 1:
                return absent(
                    f"{leg.describe()} carries more than one frequency in this window "
                    f"({', '.join(sorted(series.frequencies))}). A series whose own rows "
                    "disagree about their cadence cannot be correlated at either of them."
                )
        if left_series.frequencies != right_series.frequencies:
            return absent(
                f"{left.describe()} is {'/'.join(sorted(left_series.frequencies))} and "
                f"{right.describe()} is {'/'.join(sorted(right_series.frequencies))}. A "
                "correlation pairs two series period by period, so mixing frequencies would "
                "mean resampling one of them; it is refused rather than resampled."
            )
        for leg, series in ((left, left_series), (right, right_series)):
            if len(series.units) > 1:
                return absent(
                    f"{leg.describe()} carries more than one unit in this window "
                    f"({', '.join(sorted(series.units))}), so a change across the boundary "
                    "subtracts one quantity from another and reports the difference."
                )

        # Align on the dates BOTH series carry. Pairing by position would compare
        # a Tokyo Monday with a New York Friday.
        days = sorted(set(left_series.values) & set(right_series.values))
        own, other = paired_changes(
            [left_series.values[day] for day in days],
            [right_series.values[day] for day in days],
            left_transform=left.transform,
            right_transform=right.transform,
        )
        needed = max(self._min_observations, window)
        if len(own) < needed:
            return absent(
                f"{len(own)} overlapping observation(s), below the {needed} needed for a "
                f"{window}-day correlation",
                n_obs=len(own),
            )

        current = correlation(own[-window:], other[-window:])
        if current is None:
            return absent("one of the two series has no dispersion in this window", n_obs=len(own))

        # Non-overlapping, for the reason indicators._correlation_shift states:
        # the sd of readings sampled every session measures their smoothness.
        # Same call, same floor, so the matrix and the grid cannot disagree about
        # the same pair on the same day.
        history = rolling_correlations(
            own, other, window=window, sample=self._norm_sessions, step=window
        )
        deviation = norm_deviation(history) if len(history) >= self._norm_min_independent else None
        if deviation is None:
            # The correlation itself is measured and stays `ok`. What is missing is
            # the NORM to compare it against, and calling the whole cell absent
            # would throw away a number that was computed correctly.
            return CorrelationCell(
                left=left,
                right=right,
                window_days=window,
                correlation=current,
                norm_mean=None,
                norm_sd=None,
                z_score=None,
                flagged=False,
                status=STATUS_OK,
                n_obs=len(own),
                norm_independent=len(history),
                reason=(
                    f"correlation measured, but its {self._norm_years}-year history holds "
                    f"{len(history)} independent {window}-day reading(s) against a floor of "
                    f"{self._norm_min_independent}, or no dispersion among them. There is no "
                    "norm to deviate from and no shift to flag"
                ),
            )

        return CorrelationCell(
            left=left,
            right=right,
            window_days=window,
            correlation=current,
            norm_mean=deviation.mean,
            norm_sd=deviation.sd,
            z_score=deviation.z_score,
            flagged=abs(deviation.z_score) > self._shift_sd,
            status=STATUS_OK,
            n_obs=len(own),
            norm_independent=len(history),
        )


def _encode_trajectory(trajectory: dict[str, TrajectoryLeaf]) -> str | None:
    """The trajectory map as JSON, or None when the cell held none of the leaves.

    ``None`` rather than ``{}`` so an empty map and an unrecorded one read the
    same way a NULL column always has here: nothing was carried. Every leaf keeps
    its own status and window, which is what makes it safe to print one beside a
    percentile measured on a different cadence.
    """
    if not trajectory:
        return None
    return json.dumps({name: asdict(leaf) for name, leaf in trajectory.items()})


def _decode_object(raw: Any, what: str) -> dict[str, Any]:
    """The stored JSON, or an empty mapping when there is none or it is broken.

    A ranked row is still a ranked row without its conditions or its trajectory;
    refusing to read the whole table because one JSON blob is malformed would be
    the wrong trade.
    """
    if not raw:
        return {}
    try:
        decoded = json.loads(str(raw))
    except (TypeError, ValueError):
        log.warning("a stored ranking row carries unreadable %s; ignoring them", what)
        return {}
    return decoded if isinstance(decoded, dict) else {}


#: "YYYY-MM-DD".
_DATE_LENGTH = 10
