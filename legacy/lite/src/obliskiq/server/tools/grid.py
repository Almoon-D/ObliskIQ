"""``get_grid_rankings`` and ``get_grid_cell`` — reading the precomputed grid.

The grid is roughly ten markets by twelve sectors by fifteen indicator families:
a table to compute, not to reason through. ``obliskiq-compute`` builds it; these
tools read it. **The session reads rankings, not raw cells** — which is why the
ranked views come first here and the cell reader takes required axes rather than
offering to dump the grid.

Every cell that is not ``ok`` carries a reason naming the missing input. That
reason is returned untouched: a coverage gap reported is actionable, a coverage
gap smoothed over becomes an estimate presented as data.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from ...compute.build_grid import (
    coverage_report,
    family_reach,
    latest_grid_date,
    read_cell_history,
    read_cells,
)
from ...compute.rankings import (
    DIMENSION_LARGEST_CORRELATION_SHIFT,
    DIMENSION_MOST_COMPRESSED,
    DIMENSION_MOST_EXTENDED,
    DIMENSION_WIDEST_IV_HV,
    CorrelationMatrix,
    Rankings,
)
from ...logging_setup import get_logger
from ..context import ServerContext
from ..encode import to_jsonable
from .base import READ_ONLY, call, envelope, refuse

log = get_logger("server.tools.grid")

DIMENSIONS = (
    DIMENSION_MOST_EXTENDED,
    DIMENSION_MOST_COMPRESSED,
    DIMENSION_WIDEST_IV_HV,
    DIMENSION_LARGEST_CORRELATION_SHIFT,
)


def register(mcp: FastMCP, context: ServerContext) -> None:
    @mcp.tool(
        name="get_grid_rankings",
        description=(
            "The ranked views of the precomputed market x sector x indicator grid: "
            "most_extended, most_compressed, widest_iv_hv, largest_correlation_shift. "
            "Read these rather than the raw grid — it runs to several thousand leaf "
            "rows. Omit `dimension` for all four. Also returns the flagged "
            "cross-market correlation pairs and the grid's coverage report, whose "
            "`family_reach` says how much of the board each family actually sees: "
            "read it before treating a ranked view as a view of the whole grid."
        ),
        annotations=READ_ONLY,
    )
    async def get_grid_rankings(
        dimension: str | None = None,
        top_n: int | None = None,
        as_of: str | None = None,
        include_correlations: bool = True,
    ) -> dict[str, Any]:
        """Ranked grid views for one session.

        Args:
            dimension: One of most_extended, most_compressed, widest_iv_hv,
                largest_correlation_shift. Omit for all four.
            top_n: Rows per dimension, capped at server.max_rows_per_response.
            as_of: Session date, YYYY-MM-DD. Defaults to the latest grid build.
            include_correlations: Include correlation pairs flagged more than one
                standard deviation from their own norm. Correlation CHANGE is the
                regime signal, not correlation level.
        """
        requested = context.as_of(as_of)
        if dimension is not None and dimension not in DIMENSIONS:
            raise refuse(f"dimension {dimension!r} is not one of {list(DIMENSIONS)}")
        limit = context.bounded(top_n)

        def work() -> tuple[dict[str, Any], Any, bool]:
            with context.store() as store:
                built = latest_grid_date(store)
                if built is None:
                    raise refuse(
                        "no grid has been built. Run `obliskiq-compute grid` after the "
                        "collectors, or call run_collectors first: the grid reads only "
                        "what they wrote."
                    )
                session = built if as_of is None else requested
                rankings = Rankings(store, context.config)
                wanted = (dimension,) if dimension else DIMENSIONS

                views: dict[str, Any] = {}
                truncated = False
                for name in wanted:
                    rows = rankings.read(as_of=session, dimension=name)
                    truncated = truncated or len(rows) > limit
                    views[name] = to_jsonable(rows[:limit])

                payload: dict[str, Any] = {
                    "rankings": views,
                    "coverage": to_jsonable(coverage_report(store, as_of=session)),
                    "row_note": (
                        "each view ORDERS on one named indicator, so the ordering can be "
                        "checked against the cell it came from — read `indicator` rather "
                        "than assuming from the view's name. most_compressed orders on the "
                        "PARKINSON realised-vol percentile, which reads the intraday range "
                        "and cannot see the overnight gap, so its level is not comparable "
                        "with the close-to-close leaf; the close-to-close verdict travels "
                        "beside it as the realised_vol_compressed condition. Never subtract "
                        "a range-based figure from an implied vol. `conditions` carries the "
                        "REST of SKILL.md's definition for that view — a row in "
                        "most_compressed is not 'coiled' unless every condition there is "
                        "true, and a null condition is one the store could not measure, "
                        "never a failed one. `trajectory` carries a chosen set of leaves "
                        "from the SAME cell and the same as_of — returns at two horizons in "
                        "local and base currency, the realised-vol term slope, both 52-week "
                        "distances, the long-run drawdown, and the positioning level with "
                        "its change over each configured COT window. The ranked value is a "
                        "LEVEL; these say which way it is moving, so read the two together "
                        "and never quote a percentile without them. Each leaf carries its "
                        "OWN status, window and units, which is the only way to tell a "
                        "stale return beside a fresh percentile from a matched pair — and a "
                        "leaf the store could not measure is ABSENT from the map rather "
                        "than zero, because zero is a reading. `status` and "
                        "`market_data_status` are the "
                        "cell's own: anything stale, frozen or delayed is indicative "
                        "wherever it is quoted, including here. `reachable` and "
                        "`ranked_over` are the view's DENOMINATOR — the cells its family "
                        "can see at all, and the cells that held a usable value. The four "
                        "views print identically whatever they ordered, so a ten-row "
                        "ordering over eleven reachable cells and one over a hundred are "
                        "different claims and only these two numbers tell them apart; "
                        "`detail` says so in words below grid.min_cells_for_ranked_view."
                    ),
                    "family_reach": to_jsonable(family_reach(store, as_of=session)),
                    "family_reach_note": (
                        "how many market x sector cells each indicator family can reach at "
                        "all, ascending. A family blocked by a declaration will still be "
                        "blocked tomorrow; one merely uncollected fills in. Never rank "
                        "across families on these numbers — they say how much of the grid "
                        "a view saw, not how strong its signal is."
                    ),
                }
                if include_correlations:
                    # flagged() reads the written matrix; the market axis is only
                    # needed to BUILD one, which obliskiq-compute already did.
                    matrix = CorrelationMatrix(store, context.config, {})
                    flagged = matrix.flagged(as_of=session)
                    truncated = truncated or len(flagged) > limit
                    payload["correlations_flagged"] = to_jsonable(flagged[:limit])
                    payload["correlation_note"] = (
                        "each pair is flagged against its OWN norm, not a shared threshold, "
                        "and that norm is built from NON-OVERLAPPING readings: sampled every "
                        "session, consecutive windows share all but one observation, so "
                        "their standard deviation measures the smoothness of the series "
                        "rather than the spread of the correlation. A pair whose history "
                        "holds too few independent readings reports no shift instead of an "
                        "inflated one. A decoupling from a usual driver is the regime signal."
                    )
                if not any(views.values()):
                    payload["empty_note"] = (
                        "the grid built but no cell cleared its indicator for these "
                        "dimensions. On a young store most cells are legitimately absent; "
                        "the coverage report says how many and why."
                    )
                return payload, session, truncated

        data, session, truncated = await call("get_grid_rankings", work)
        return envelope(
            context, as_of=requested, data=data, source_as_of=session, truncated=truncated
        )

    @mcp.tool(
        name="get_grid_cell",
        description=(
            "Specific grid cells with their window, units, status, n_obs and — when "
            "absent — the reason naming the missing input. Narrow by market, sector "
            "and/or indicator; at least one is required. Each cell carries the "
            "instrument that measured it, including ETF concentration and hedged flag "
            "where the measurement instrument is a wrapper rather than a native index. "
            "Pass `history` with all three axes named to get that ONE cell across the "
            "sessions the grid has accumulated, newest first — which is the only way "
            "to see a reading move, as everything else here is a single snapshot."
        ),
        annotations=READ_ONLY,
    )
    async def get_grid_cell(
        market: str | None = None,
        sector: str | None = None,
        indicator: str | None = None,
        as_of: str | None = None,
        history: int | None = None,
    ) -> dict[str, Any]:
        """One market / sector / indicator slice of the grid.

        Args:
            market: Market key as declared in universe.yaml, e.g. "korea".
            sector: Sector key, or "broad" for the market-level row.
            indicator: Indicator name, e.g. "iv_minus_hv" or "distance_from_52w_high".
            as_of: Session date, YYYY-MM-DD. Defaults to the latest grid build.
            history: Return this many sessions of ONE cell, newest first, instead of
                one session's slice. Requires market, sector and indicator together,
                because a series is only a series for a single cell.
        """
        requested = context.as_of(as_of)
        if history is not None:
            if not (market and sector and indicator):
                raise refuse(
                    "history reads ONE cell across sessions, so market, sector and "
                    "indicator are all required. Without all three the result would "
                    "interleave different measurements under one date order and read "
                    "as a series that is not one."
                )
            if history <= 0:
                raise refuse("history must be a positive number of sessions.")

            def history_work() -> tuple[dict[str, Any], Any, bool]:
                with context.store() as store:
                    limit = min(int(history), context.max_rows)
                    cells = read_cell_history(
                        store,
                        market=market,
                        sector=sector,
                        indicator=indicator,
                        until=None if as_of is None else requested,
                        limit=limit,
                    )
                    payload: dict[str, Any] = {
                        "cells": to_jsonable(cells),
                        "count": len(cells),
                        "history_note": (
                            "newest first. A session where this cell was not measured is "
                            "present with its own status and reason rather than skipped, "
                            "so a gap in the middle is visible as a gap. Compare values "
                            "only where `window` and `units` match across the rows: the "
                            "grid does not re-measure history, it accumulates it, so a "
                            "window that changed between builds makes two rows "
                            "incomparable even under one indicator name."
                        ),
                    }
                    if not cells:
                        payload["empty_note"] = (
                            "this cell has no rows at any session. Check the axis names "
                            "against universe.yaml; the grid writes every declared cell, "
                            "so an absent one is a naming mismatch rather than a gap."
                        )
                    newest = cells[0].as_of if cells else None
                    return payload, newest, len(cells) >= limit

            data, session, truncated = await call("get_grid_cell", history_work)
            return envelope(
                context, as_of=requested, data=data, source_as_of=session, truncated=truncated
            )

        if market is None and sector is None and indicator is None:
            raise refuse(
                "name at least one of market, sector or indicator. The grid runs to "
                "several thousand leaf rows — every declared market x every sector x "
                "the leaves the fifteen families emit — and the session reads rankings, not the "
                "raw table. Use get_grid_rankings for the ranked views. Naming a "
                "market alone still exceeds one response: narrow by indicator too, or "
                "read the `truncated` flag."
            )

        def work() -> tuple[dict[str, Any], Any, bool]:
            with context.store() as store:
                built = latest_grid_date(store)
                if built is None:
                    raise refuse("no grid has been built. Run `obliskiq-compute grid` first.")
                session = built if as_of is None else requested
                limit = context.max_rows
                cells = read_cells(
                    store,
                    as_of=session,
                    market=market,
                    sector=sector,
                    indicator=indicator,
                    limit=limit + 1,
                )
                truncated = len(cells) > limit
                cells = cells[:limit]
                payload: dict[str, Any] = {"cells": to_jsonable(cells), "count": len(cells)}
                # Every cell carries the market-data state of the quotes behind
                # it. SKILL.md requires anything FROZEN or DELAYED to be labelled
                # indicative in every table it appears in, and this is the table.
                # A NULL is a macro cell, which reads a published series and has
                # no quote state, or a route that reported nothing — never an
                # assertion that the quote was live.
                statuses: dict[str, int] = {}
                for cell in cells:
                    if cell.market_data_status:
                        statuses[cell.market_data_status] = (
                            statuses.get(cell.market_data_status, 0) + 1
                        )
                if statuses:
                    payload["market_data_statuses"] = statuses
                    payload["market_data_note"] = (
                        "cells carrying a market_data_status not on "
                        "data.market_data_status_live are INDICATIVE, not live. Label them "
                        "so in every table they appear in, and never present them beside "
                        "live figures unmarked."
                    )
                if not cells:
                    payload["empty_note"] = (
                        "no cell matches. A cell absent from the grid is different from a "
                        "cell written as n/a: check the axis names against universe.yaml, "
                        "then the coverage report from get_grid_rankings."
                    )
                return payload, session, truncated

        data, session, truncated = await call("get_grid_cell", work)
        return envelope(
            context, as_of=requested, data=data, source_as_of=session, truncated=truncated
        )
