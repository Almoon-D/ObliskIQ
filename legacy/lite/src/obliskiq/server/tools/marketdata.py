"""``record_iv_percentile`` — the bridge that gives grid indicator 8 a source.

Indicator 8 wants a 52-week implied-volatility percentile per underlying, and
until now nothing could put one in the store.

The figure exists. IBKR serves ``implied_volatility_percentile`` as
``{high_13w, high_26w, high_52w}`` — verified live, **on the MCP connector**,
and recorded in ``references/ibkr-playbook.md``. The collectors do not run
there: they run on the Client Portal Web
API, where no field id for the percentile has been verified, so
``ibkr.cp_api.marketdata.underlying_snapshot_fields.implied_vol_percentile`` is
``null`` and every unattended write leaves the column empty. Guessing an id is
specifically forbidden — ``7633`` is *Implied Vol./Historical Vol %*, a ratio,
and it would land in a percentile column looking entirely plausible.

That left a session able to READ the number and with nowhere to put it: the
skill could see a percentile the gates could not use. This tool is the missing
half. It accepts the vendor figure from a caller that has the connector, records
which route it came from, and leaves everything else about the pipeline alone.

Two refusals matter more than the write itself.

**A percentile outside 0-1 is rejected, never rescaled.** The vendor figure is
already a fraction — the playbook's standing warning is against dividing it by a
hundred — so a caller passing ``94`` for the 94th percentile has misread
something. Quietly dividing would make the misreading invisible, and this column
is one nobody ever eyeballs.

**A supplied field never blanks one already collected.** The upsert writes only
what it was given, so recording a percentile beside a collector's implied vol
keeps both — and it does not relabel the row's ``source`` either. One row can
hold fields from two routes, so the percentile's own route goes in
``iv_percentile_route`` and the row keeps the ``source`` of whatever created it.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from ...compute.frames import contract_id_for_symbol
from ...logging_setup import get_logger
from ...store.db import Store
from ...util.timeutil import iso_date
from ..context import ServerContext
from ..encode import to_jsonable
from ..guards import parse_optional_date
from .base import UPSERT, call, envelope, refuse

log = get_logger("server.tools.marketdata")

#: The routes a row may be attributed to, matching the store's own CHECK.
SOURCES = ("mcp", "cp_api")

#: A percentile is a fraction of its window. Both ends inclusive: a reading can
#: legitimately sit at the very top or bottom of 52 weeks.
PERCENTILE_MIN = 0.0
PERCENTILE_MAX = 1.0


def register(mcp: FastMCP, context: ServerContext) -> None:
    @mcp.tool(
        name="record_iv_percentile",
        description=(
            "Record a VENDOR implied-volatility percentile for an underlying, with the "
            "route it came from. This is how the IBKR MCP connector's "
            "implied_volatility_percentile ({high_13w, high_26w, high_52w}) reaches the "
            "store: the collectors run on Client Portal, where no field id for it has "
            "been verified, so without this call grid indicator 8 stays n/a. Values are "
            "FRACTIONS (0.12, not 12) and anything outside 0-1 is REFUSED rather than "
            "rescaled — the vendor figure is already a fraction, and a silently divided "
            "misreading lands in a column nobody ever eyeballs. Upserts on "
            "(snapshot_date, contract_id) and never blanks a field it was not given."
        ),
        annotations=UPSERT,
    )
    async def record_iv_percentile(
        iv_percentile_52w: float,
        symbol: str | None = None,
        contract_id: int | None = None,
        iv_percentile_13w: float | None = None,
        iv_percentile_26w: float | None = None,
        implied_vol: float | None = None,
        historical_vol_30d: float | None = None,
        last: float | None = None,
        snapshot_date: str | None = None,
        source: str = "mcp",
        as_of: str | None = None,
    ) -> dict[str, Any]:
        """Store a vendor IV percentile against an underlying.

        Args:
            iv_percentile_52w: The 52-week percentile as a FRACTION, 0-1. This is
                the window data.iv_percentile_window_weeks names and the one the
                gates read. Outside 0-1 the call is refused.
            symbol: The underlying's ticker, resolved against the universe table.
                Supply this or contract_id.
            contract_id: The underlying's conid, if already known. Wins over
                symbol when both are given.
            iv_percentile_13w: The 13-week percentile, same units. Kept because
                the connector serves it and a past date cannot be re-fetched.
            iv_percentile_26w: The 26-week percentile, same units.
            implied_vol: Current implied vol for the underlying, decimal.
            historical_vol_30d: Realised vol from the same snapshot, decimal.
            last: Last traded price, so the row carries its own spot.
            snapshot_date: The session this reading is for, YYYY-MM-DD. Defaults
                to as_of. Back-date it honestly: a percentile filed under the
                wrong day is worse than one not filed.
            source: Which route served it, "mcp" or "cp_api". Recorded per row so
                collection can be migrated wholesale later.
            as_of: Session date, YYYY-MM-DD. Defaults to today in UTC.
        """
        session = context.as_of(as_of)
        stamp = parse_optional_date(snapshot_date, label="snapshot_date") or session

        if source not in SOURCES:
            raise refuse(f"source {source!r} is not one of {list(SOURCES)}")

        percentiles = {
            "iv_percentile_52w": iv_percentile_52w,
            "iv_percentile_26w": iv_percentile_26w,
            "iv_percentile_13w": iv_percentile_13w,
        }
        for name, value in percentiles.items():
            if value is not None and not PERCENTILE_MIN <= value <= PERCENTILE_MAX:
                raise refuse(
                    f"{name} is {value!r}, outside {PERCENTILE_MIN}-{PERCENTILE_MAX}. IBKR "
                    "serves this as a fraction already, so a value above 1 is either a "
                    "percent figure or some other quantity entirely. It is refused rather "
                    "than divided: a rescaled misreading is invisible in a column nobody "
                    "eyeballs, and dividing the wrong quantity by a hundred does not make "
                    "it a percentile."
                )

        # The two companion volatility fields go through the SAME plausibility
        # thresholds the collector's own decoder applies to the identical
        # columns — ibkr/cp_client.py's _decode_implied_vol and
        # _reject_implausible. Without this, a caller reaches through this
        # tool's separate write path and lands IBKR's own -1.0 "no value here"
        # sentinel, or a unit-error magnitude, in the exact column the
        # collector was built to keep them out of.
        md_cfg = context.config.section("ibkr.cp_api.marketdata")
        iv_floor = md_cfg.require_float("implied_vol_min_plausible")
        iv_ceiling = md_cfg.require_float("implied_vol_max_plausible")
        volatility_fields = (
            ("implied_vol", implied_vol),
            ("historical_vol_30d", historical_vol_30d),
        )
        for name, value in volatility_fields:
            if value is not None and not iv_floor <= value <= iv_ceiling:
                raise refuse(
                    f"{name} is {value!r}, outside {iv_floor}-{iv_ceiling} "
                    "(ibkr.cp_api.marketdata.implied_vol_min_plausible / "
                    "_max_plausible). A volatility has no negative branch and IBKR is "
                    "known to serve -1.0 as a sentinel for 'no value here', so a "
                    "reading outside this band is a unit error or a sentinel, not a "
                    "quote — the same test the collector's own decoder applies to "
                    "this column."
                )
        # A spot price cannot be negative. This is a structural fact about what
        # `last` means, not a tunable threshold, so it takes no config key —
        # the same treatment _decode_dividend_yield gives a negative yield.
        if last is not None and last < 0.0:
            raise refuse(f"last is {last!r}, which is negative. A traded price cannot be.")

        def work() -> dict[str, Any]:
            with context.store() as store:
                resolved = _resolve(store, contract_id=contract_id, symbol=symbol)
                written = _upsert_snapshot(
                    store,
                    snapshot_date=iso_date(stamp),
                    contract_id=resolved,
                    source=source,
                    values={
                        **percentiles,
                        "iv_percentile_route": source,
                        "implied_vol": implied_vol,
                        "historical_vol_30d": historical_vol_30d,
                        "last": last,
                    },
                )
                log.info(
                    "vendor iv percentile recorded",
                    extra={
                        "contract_id": resolved,
                        "snapshot_date": iso_date(stamp),
                        "source": source,
                    },
                )
                return {
                    "contract_id": resolved,
                    "snapshot_date": iso_date(stamp),
                    "source": source,
                    "fields_written": sorted(written),
                    "row": to_jsonable(_read_back(store, iso_date(stamp), resolved)),
                    "note": (
                        "recorded as a VENDOR reading. It stays the source until "
                        "data.iv_self_source_min_sessions of self-collected snapshots "
                        "exist, per data.iv_percentile_source."
                    ),
                }

        return envelope(context, as_of=session, data=await call("record_iv_percentile", work))


def _resolve(store: Store, *, contract_id: int | None, symbol: str | None) -> int:
    """The underlying this reading belongs to, as the universe knows it."""
    if contract_id is not None:
        row = store.execute(
            "SELECT contract_id FROM universe WHERE contract_id = ?", (contract_id,)
        ).fetchone()
        if row is None:
            raise refuse(
                f"contract {contract_id} is not in the universe table. Seed it with "
                "`obliskiq-collect universe` first: a percentile filed against an "
                "instrument nobody can classify is a number with no cell to sit in."
            )
        return int(row[0])
    if symbol is None or not symbol.strip():
        raise refuse("supply either symbol or contract_id")
    resolved = contract_id_for_symbol(store, symbol.strip())
    if resolved is None:
        raise refuse(
            f"{symbol!r} is not in the universe table, so this reading has no cell to "
            "sit in. Seed it with `obliskiq-collect universe` first."
        )
    return resolved


def _upsert_snapshot(
    store: Store,
    *,
    snapshot_date: str,
    contract_id: int,
    source: str,
    values: dict[str, Any],
) -> list[str]:
    """Write only the fields supplied, leaving the rest of the row as it was.

    A blanket upsert would set every unsupplied column to NULL, so recording a
    percentile would erase the implied vol the chain collector wrote that
    morning. The columns are keys of ``values``, never caller input.

    ``source`` is set on INSERT — this call created the row, so it owns the
    label — and deliberately left alone on UPDATE. Overwriting it would relabel
    a collector's quote as MCP-served because a percentile arrived beside it
    later. The percentile's own route travels in ``iv_percentile_route``.
    """
    supplied = {name: value for name, value in values.items() if value is not None}
    columns = ", ".join(supplied)
    placeholders = ", ".join("?" for _ in supplied)
    updates = ", ".join(f"{name} = excluded.{name}" for name in supplied)
    with store.transaction() as connection:
        connection.execute(
            f"INSERT INTO underlying_snapshots (snapshot_date, contract_id, source, {columns}) "
            f"VALUES (?, ?, ?, {placeholders}) "
            f"ON CONFLICT (snapshot_date, contract_id) DO UPDATE SET {updates}",
            (snapshot_date, contract_id, source, *supplied.values()),
        )
    return list(supplied)


def _read_back(store: Store, snapshot_date: str, contract_id: int) -> dict[str, Any] | None:
    row = store.execute(
        "SELECT * FROM underlying_snapshots WHERE snapshot_date = ? AND contract_id = ?",
        (snapshot_date, contract_id),
    ).fetchone()
    return None if row is None else dict(row)
