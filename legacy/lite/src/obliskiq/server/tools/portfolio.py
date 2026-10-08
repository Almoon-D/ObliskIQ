"""``get_portfolio_review`` — holdings joined to the ledger, thesis verbatim.

Positions arrive as an argument rather than being fetched. The computation layer
touches no network by design — the systemd unit enforces it with
``PrivateNetwork=true`` — and the session already holds a live IBKR connector.
So the caller passes what ``get_account_positions`` returned and this tool does
what only the store can: join it to ``position_ledger``, fold in the manually
entered non-IBKR holdings, compute stops and cap utilisation over the whole
book, and read each thesis back **verbatim from the row written at entry**.

Verbatim is the point. A thesis restated from memory or reconstructed from the
current price is how a position bought for a product story becomes a position
held for cheap implied vol without anybody noticing the substitution. The
would-I-buy-today test only means something against the original words.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

from datetime import date
from typing import Any

from mcp.server.fastmcp import FastMCP

from ...compute.frames import bar_series, latest_nav, universe_row
from ...compute.risk_engine import RiskEngine, StopLevels, holdings_from_rows
from ...logging_setup import get_logger
from ...store.db import Store
from ...store.ledger import Position, open_positions
from ...util.timeutil import parse_date
from ..context import ServerContext
from ..encode import to_jsonable
from .base import READ_ONLY, call, envelope

log = get_logger("server.tools.portfolio")


def register(mcp: FastMCP, context: ServerContext) -> None:
    @mcp.tool(
        name="get_portfolio_review",
        description=(
            "Open positions joined to position_ledger: original thesis VERBATIM, "
            "invalidation trigger, target, time window, mandatory close date, days "
            "held, volatility-scaled stops, and cap utilisation across position, "
            "sector, country and currency. Pass `positions` from IBKR "
            "get_account_positions; manually entered non-IBKR holdings are folded in "
            "from the store. Caps are UNRELIABLE rather than wrong when external "
            "holdings are stale."
        ),
        annotations=READ_ONLY,
    )
    async def get_portfolio_review(
        positions: list[dict[str, Any]] | None = None,
        nav: float | None = None,
        as_of: str | None = None,
        include_closed: bool = False,
    ) -> dict[str, Any]:
        """The portfolio review table, with every thesis read back as written.

        Args:
            positions: Rows from IBKR get_account_positions. Each needs at least
                `symbol` and `market_value_base`; `sector`, `country`, `currency`,
                `contract_id`, `cost_basis_base` and `nav_at_purchase` sharpen the
                cap report. Caps measure against NAV at PURCHASE date, so
                `nav_at_purchase` is what makes the initial-size cap meaningful.
            nav: Total NAV in base currency. Defaults to the store's latest.
            as_of: Session date, YYYY-MM-DD. Defaults to today in UTC.
            include_closed: Also return closed ledger rows, for reconciliation.
        """
        session = context.as_of(as_of)

        def work() -> dict[str, Any]:
            with context.store() as store:
                engine = RiskEngine(store, context.config)
                resolved_nav = nav if nav is not None else latest_nav(store)
                held = open_positions(store)
                supplied = holdings_from_rows(positions or [])
                external, stale = engine.external_holdings(as_of=session)

                payload: dict[str, Any] = {
                    "ledger": [
                        _review_row(store, engine, position, as_of=session) for position in held
                    ],
                    "external_positions": to_jsonable(external),
                    "ibkr_positions_supplied": len(supplied),
                }

                if stale:
                    payload["external_stale"] = list(stale)

                if resolved_nav is None:
                    payload["caps"] = None
                    payload["caps_note"] = (
                        "no NAV was supplied and none is recorded, so no cap can be "
                        "computed. A cap reported without NAV is a number nobody can "
                        "defend."
                    )
                elif not supplied and not external:
                    payload["caps"] = None
                    payload["caps_note"] = (
                        "no holdings were supplied and none are entered manually, so "
                        "there is nothing to measure against the caps. Pass positions "
                        "from get_account_positions."
                    )
                else:
                    report = engine.cap_utilisation(supplied, nav=resolved_nav, as_of=session)
                    payload["caps"] = to_jsonable(report)
                    payload["caps_reliable"] = report.reliable
                    payload["winners_are_never_trimmed"] = not engine.rebalance_winners
                    payload["breaker"] = to_jsonable(
                        engine.circuit_breaker(as_of=session, nav=resolved_nav)
                    )

                if include_closed:
                    payload["closed"] = to_jsonable(_closed_rows(store, limit=context.max_rows))

                if not held:
                    payload["ledger_note"] = (
                        "no open position carries a ledger row. A holding with no logged "
                        "thesis cannot be reviewed against the would-I-buy-today test — "
                        "log_entry writes the thesis and it is frozen thereafter."
                    )
                return payload

        return envelope(context, as_of=session, data=await call("get_portfolio_review", work))


def _review_row(
    store: Store, engine: RiskEngine, position: Position, *, as_of: date
) -> dict[str, Any]:
    """One ledger row, with everything the review table quotes from it."""
    opened = parse_date(position.opened_at)
    universe = universe_row(store, position.contract_id) or {}
    row: dict[str, Any] = {
        "position_id": position.position_id,
        "contract_id": position.contract_id,
        "symbol": universe.get("symbol"),
        "opened_at": position.opened_at,
        "days_held": None if opened is None else (as_of - opened).days,
        "entry_price": position.entry_price,
        "entry_range_pctile": position.entry_range_pctile,
        # Verbatim, from the row written at entry. Never restated.
        "thesis_text": position.thesis_text,
        "invalidation_trigger": position.invalidation_trigger,
        "target": position.target,
        "time_window_end": position.time_window_end,
        "mandatory_close_date": position.mandatory_close_date,
        "prob_target_at_entry": position.prob_target_at_entry,
        "reunderwrite_log": to_jsonable(position.reunderwrite_log),
        "thesis_is_frozen": True,
    }
    stops = _stops_for(store, engine, position)
    row["stops"] = to_jsonable(stops)
    if stops is None:
        row["stops_note"] = (
            "not enough daily history to compute ATR, so no stop is offered. A "
            "fixed-percentage stop is not produced as a substitute: it is noise on a "
            "high-volatility name and far too loose on a quiet one."
        )
    return row


def _stops_for(store: Store, engine: RiskEngine, position: Position) -> StopLevels | None:
    bars = bar_series(store, position.contract_id)
    if not bars.close:
        return None
    trailing_high = bars.trailing_high
    return engine.equity_stops(bars, entry_price=position.entry_price, trailing_high=trailing_high)


def _closed_rows(store: Store, *, limit: int) -> list[dict[str, Any]]:
    rows = store.execute(
        "SELECT position_id, contract_id, opened_at, closed_at, exit_reason, realised_pnl "
        "FROM position_ledger WHERE closed_at IS NOT NULL ORDER BY closed_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]
