"""``get_sleeve_status`` — what the sleeve can spend, before anything is screened.

The first thing a session reports, and deliberately the first tool here. The
most repeated failure mode in this account is discovering the budget last: three
consecutive sessions produced full global analyses and then found the option
budget could not reach any top candidate. The budget defines the universe, so it
is computed first and the universe is filtered by it.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from ...compute.frames import latest_nav_observation
from ...compute.penalty import PenaltyLedger
from ...compute.screen import OptionScreen
from ...logging_setup import get_logger
from ...util.timeutil import iso_date
from ..context import ServerContext
from ..encode import to_jsonable
from ..guards import parse_optional_date
from .base import READ_ONLY, call, envelope, refuse

log = get_logger("server.tools.sleeve")


def register(mcp: FastMCP, context: ServerContext) -> None:
    @mcp.tool(
        name="get_sleeve_status",
        description=(
            "Available option budget, penalty balance outstanding, realised gains still "
            "required to clear it, and the affordability ceiling that implies. Call this "
            "FIRST in any session: the budget defines the universe, and screening before "
            "computing it is the most repeated failure mode in this account. NAV is taken "
            "from the store's latest observation unless supplied."
        ),
        annotations=READ_ONLY,
    )
    async def get_sleeve_status(
        nav: float | None = None,
        as_of: str | None = None,
        sigma: float | None = None,
        tau_years: float | None = None,
    ) -> dict[str, Any]:
        """Sleeve budget, penalty balance and the affordability ceiling.

        Args:
            nav: Total NAV in base currency, IBKR plus manually entered holdings.
                Defaults to the latest value recorded in the store.
            as_of: Session date, YYYY-MM-DD. Defaults to today in UTC.
            sigma: Annualised volatility of a candidate underlying. Supplying it
                with ``tau_years`` returns the maximum underlying price a single
                at-the-money contract still fits, which is the filter to apply
                before screening.
            tau_years: Time to expiry in years, for the same estimate.
        """
        session = context.as_of(as_of)

        def work() -> dict[str, Any]:
            with context.store() as store:
                observation = None if nav is not None else latest_nav_observation(store)
                resolved = (
                    nav if nav is not None else (None if observation is None else observation.nav)
                )
                if resolved is None:
                    raise refuse(
                        "no NAV was supplied and none is recorded in nav_history, so the "
                        "sleeve budget cannot be computed. Pass nav from the account, or "
                        "record one first. The budget defines the universe; a session that "
                        "screens without it is the failure this tool exists to prevent."
                    )
                penalty = PenaltyLedger(store, context.config)
                screen = OptionScreen(store, context.config)
                status = penalty.status(resolved)
                ceiling = screen.affordability(resolved)
                discrepancies = penalty.verify()

                payload: dict[str, Any] = {
                    "sleeve": to_jsonable(status),
                    "locked": status.is_locked,
                    # Two causes, opposite remedies: a penalty lock clears only
                    # on realised qualifying gains, a commitment lock clears the
                    # moment a position closes. Reporting "locked" alone would
                    # send a session looking for the wrong one.
                    "locked_by": status.locked_by,
                    "affordability": to_jsonable(ceiling),
                    "nav_source": "supplied" if nav is not None else "nav_history",
                    "nav_as_of": (
                        None
                        if observation is None or observation.as_of is None
                        else iso_date(observation.as_of)
                    ),
                    "recent_events": to_jsonable(penalty.events()[-context.bounded(None) :]),
                }
                if discrepancies:
                    # Report findings rather than quietly reconciling them.
                    payload["ledger_discrepancies"] = discrepancies
                if sigma is not None and tau_years is not None:
                    payload["max_underlying_price"] = ceiling.max_underlying_price(
                        sigma=sigma, tau_years=tau_years
                    )
                    payload["max_underlying_price_note"] = (
                        "an at-the-money estimate for filtering a universe, never a quote. "
                        "A candidate surviving it still has to clear the real premium."
                    )
                return payload

        payload = await call("get_sleeve_status", work)
        # A stored NAV carries its own date through the envelope, so a budget
        # sized on a months-old reading is labelled rather than merely returned.
        # A NAV supplied by the caller is today's by construction.
        source = parse_optional_date(payload.pop("nav_as_of", None), label="nav_as_of")
        return envelope(context, as_of=session, data=payload, source_as_of=source)
