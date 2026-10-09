"""``screen_options`` — the candidates that survive every gate at once.

Each gate on its own looks survivable; the intersection is what the sleeve can
actually trade, and it is much smaller than any single count suggests. This runs
the same evaluation the weekly census runs, so a contract cannot pass the
screener and fail the census or the other way round.

The affordability ceiling is applied **before** anything else. Discovering the
budget last is the most repeated failure mode in this account — three sessions
produced full global analyses and then found the budget could not reach any top
candidate.

Survivors come back split cheap / expensive / coiled / extended, and a contract
whose implied-vol percentile was never served is reported as unclassified with
its gate left untested rather than assumed ordinary. An untested gate is not a
passed gate.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from ...compute.frames import QUALITY_LIVE
from ...compute.screen import OptionScreen
from ...compute.universe_census import UniverseCensus
from ...logging_setup import get_logger
from ..context import ServerContext
from ..encode import to_jsonable
from .base import READ_ONLY, call, envelope

log = get_logger("server.tools.screen")


def register(mcp: FastMCP, context: ServerContext) -> None:
    @mcp.tool(
        name="screen_options",
        description=(
            "Option candidates surviving EVERY gate at once — affordability ceiling, "
            "liquidity (spread, open interest, session volume), tenor window, IV "
            "percentile, and the prohibited-instrument rules including PRIIPs. Returns "
            "survivors split cheap / expensive / coiled / extended, ranked on IV minus "
            "HV, plus the latest all-gates census verdict. The budget is applied before "
            "screening, not after."
        ),
        annotations=READ_ONLY,
    )
    async def screen_options(
        symbols: list[str] | None = None,
        nav: float | None = None,
        as_of: str | None = None,
        bucket: str | None = None,
        limit: int | None = None,
        include_incomplete: bool = True,
    ) -> dict[str, Any]:
        """The dislocation screen, gated.

        Args:
            symbols: Restrict to these underlying symbols. Omit for the whole
                collected universe.
            nav: Total NAV in base currency. Defaults to the store's latest;
                without one the affordability ceiling cannot be computed and the
                screen refuses rather than screening unbounded.
            as_of: Session date, YYYY-MM-DD. Defaults to today in UTC.
            bucket: Return only one of cheap, expensive, coiled, extended.
            limit: Maximum survivors, capped at server.max_rows_per_response.
            include_incomplete: Also return contracts that failed nothing but
                left a gate unmeasured, reported separately.
        """
        session = context.as_of(as_of)
        rows = context.bounded(limit)

        def work() -> tuple[dict[str, Any], Any, bool]:
            with context.store() as store:
                screen = OptionScreen(store, context.config)
                result = screen.screen(as_of=session, nav=nav, symbols=symbols, limit=rows)

                if result.reason is not None:
                    return (
                        {
                            "survivors": [],
                            "blocked": result.reason,
                            "contracts_scanned": 0,
                        },
                        result.snapshot_date,
                        False,
                    )

                buckets = result.by_bucket()
                selected = buckets.get(bucket, []) if bucket is not None else list(result.survivors)
                payload: dict[str, Any] = {
                    "contracts_scanned": result.contracts_scanned,
                    "nav": result.nav,
                    "affordability": to_jsonable(result.ceiling),
                    "survivors": to_jsonable(selected),
                    "counts_by_bucket": {name: len(rows_) for name, rows_ in buckets.items()},
                    "ranked_on": "iv_minus_hv",
                    # Per SKILL.md's freshness rule: anything FROZEN or DELAYED is
                    # indicative and must be labelled so in every table it appears
                    # in. Each row carries its own status; this is the table's.
                    "data_quality": {
                        "market_data_quality": result.market_data_quality,
                        "market_data_statuses": result.market_data_statuses,
                        "snapshot_date": (
                            None
                            if result.snapshot_date is None
                            else result.snapshot_date.isoformat()
                        ),
                    },
                }
                if result.market_data_quality is not None and (
                    result.market_data_quality != QUALITY_LIVE
                ):
                    payload["data_quality_note"] = (
                        "this table is INDICATIVE, not live: at least one quote behind it "
                        "is not on data.market_data_status_live. Label it as such wherever "
                        "it is presented, and never mix it with live quotes unmarked. Each "
                        "row carries its own market_data_status."
                    )
                if bucket is not None:
                    payload["bucket"] = bucket
                if include_incomplete and result.incomplete:
                    payload["passed_with_untested_gates"] = to_jsonable(result.incomplete)
                    payload["untested_note"] = (
                        "these failed nothing, but at least one gate had no input and was "
                        "never applied. An untested gate is not a passed gate."
                    )

                census = UniverseCensus(store, context.config).history(limit=1)
                if census:
                    payload["latest_census"] = to_jsonable(census[0])
                if not selected:
                    payload["empty_note"] = (
                        "nothing clears every gate on the latest snapshot. This is a "
                        "finding, not a prompt to relax a threshold: either the gates are "
                        "miscalibrated or the sleeve is too small for this universe, and "
                        "neither is fixed by loosening one. Not spending is normal."
                    )
                return payload, result.snapshot_date, result.truncated

        data, snapshot, truncated = await call("screen_options", work)
        return envelope(
            context, as_of=session, data=data, source_as_of=snapshot, truncated=truncated
        )
