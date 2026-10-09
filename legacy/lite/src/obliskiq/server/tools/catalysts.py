"""``get_catalyst_calendar`` — dated events, and what still needs verifying.

Every event returned is derived from something already written down: a mandatory
close date computed at entry, a time window pre-committed at entry, an option
expiry from a collected chain, a catalyst cited in a logged re-underwrite. The
server has no network, so it cannot confirm an earnings date and does not invent
one. The classes it cannot verify come back named, with where to confirm them.

That is the whole design. "Recalled event dates" is on this account's list of
things that have already gone wrong — an earnings date stated five days early on
the most time-sensitive position in the book — and a calendar that filled the
gap with a plausible date would repeat it with more authority behind it.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from ...compute.catalysts import CatalystCalendarBuilder
from ...logging_setup import get_logger
from ..context import ServerContext
from ..encode import to_jsonable
from .base import READ_ONLY, call, envelope

log = get_logger("server.tools.catalysts")


def register(mcp: FastMCP, context: ServerContext) -> None:
    @mcp.tool(
        name="get_catalyst_calendar",
        description=(
            "Dated events across holdings and watchlist that the store can defend: "
            "mandatory close dates and time windows pre-committed at entry, collected "
            "option expiries, and catalysts cited in logged re-underwrites. ALSO returns "
            "the event classes this server cannot verify offline — earnings, index "
            "events, central-bank dates, corporate actions — with where to confirm each. "
            "Confirm those at source before using them; do not treat their absence here "
            "as their absence in the world."
        ),
        annotations=READ_ONLY,
    )
    async def get_catalyst_calendar(
        as_of: str | None = None,
        through_days: int | None = None,
        watchlist: list[str] | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """The derivable calendar, plus an honest account of the gaps.

        Args:
            as_of: Session date, YYYY-MM-DD. Defaults to today in UTC.
            through_days: How far ahead to look. Defaults to tenor.entry_dte_max,
                the longest hold an entry today could still have.
            watchlist: Extra symbols to include option expiries for, beyond the
                contracts already held.
            limit: Maximum events, capped at server.max_rows_per_response.
        """
        session = context.as_of(as_of)
        rows = context.bounded(limit)

        def work() -> dict[str, Any]:
            with context.store() as store:
                builder = CatalystCalendarBuilder(store, context.config)
                calendar = builder.build(
                    as_of=session,
                    through_days=through_days,
                    watchlist=watchlist,
                    limit=rows,
                )
                payload: dict[str, Any] = {
                    "calendar": to_jsonable(calendar),
                    "summary": calendar.summary(),
                    "verification_required": (
                        "every date above is derived from a ledger row or a collected "
                        "contract term. Confirm earnings against company IR and index "
                        "changes against the provider — INCLUDING the mechanical timing: "
                        "trackers rebalance in the closing auction of the day before an "
                        "effective date, so the actionable window is earlier than the "
                        "headline."
                    ),
                }
                if calendar.positions_without_events:
                    payload["positions_without_events_note"] = (
                        "these open positions have no dated event inside the window. A "
                        "position whose only exit is a price level has no calendar, which "
                        "is worth stating rather than leaving to inference."
                    )
                return payload

        data = await call("get_catalyst_calendar", work)
        return envelope(
            context,
            as_of=session,
            data=data,
            truncated=bool(data["calendar"].get("truncated")),
        )
