"""``get_commodity_state`` — curve, positioning, inventory, proxies, wrapper.

Every commodity view carries a **wrapper recommendation**, because term
structure decides the instrument and not just the view. Beyond
``commodities.term_structure.contango_max_annualised_for_futures_wrapper`` a
futures-tracking product bleeds roll continuously over a 90-200 day hold whether
or not the thesis is right, and the view belongs in producer equity or a
physically-backed ETC instead.

Two separations the engine enforces and this tool preserves in its output:
positioning percentiles and cycle position are different types over different
windows — a five-year percentile is never cycle position on an asset with
10-20 year cycles — and a proxy below ``commodities.proxies.min_r2`` is returned
flagged unreliable rather than presented as commodity exposure.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from ...compute.commodity_engine import CommodityEngine
from ...logging_setup import get_logger
from ..context import ServerContext
from ..encode import to_jsonable
from .base import READ_ONLY, call, envelope, refuse

log = get_logger("server.tools.commodity")


def register(mcp: FastMCP, context: ServerContext) -> None:
    @mcp.tool(
        name="get_commodity_state",
        description=(
            "Curve shape and annualised roll, contango/backwardation classification, "
            "CFTC positioning percentile, long-window cycle position, EIA inventory "
            "deviation from the 5-year SEASONAL band, equity-proxy beta and R-squared, "
            "and a WRAPPER RECOMMENDATION. Check the curve before choosing an "
            "instrument, every time: beyond the contango ceiling a futures-tracking "
            "wrapper is rejected. Omit `root` to list every tracked root."
        ),
        annotations=READ_ONLY,
    )
    async def get_commodity_state(
        root: str | None = None,
        as_of: str | None = None,
        include_proxies: bool = True,
        include_agri_linkage: bool = False,
    ) -> dict[str, Any]:
        """One commodity's full state, or the tracked roots.

        Args:
            root: Futures root as declared in universe.yaml, e.g. "CL" or "HG".
                Omit to list every tracked root with its bucket and tradability.
            as_of: Session date, YYYY-MM-DD. Defaults to today in UTC.
            include_proxies: Include equity proxies with their computed beta and
                R-squared against the reference series. A proxy below the R-squared
                floor is returned flagged, not silently dropped.
            include_agri_linkage: Include the fertiliser-to-crop lag linkage.
                Only meaningful for a crop root.
        """
        session = context.as_of(as_of)

        def work() -> dict[str, Any]:
            with context.store() as store:
                engine = CommodityEngine(store, context.config)
                if root is None:
                    return {
                        "roots": to_jsonable(engine.tracked_roots()),
                        "note": (
                            "name a root for its full state. Commodities are never one "
                            "bucket: gold and silver are monetary assets driven by real "
                            "yields and the dollar, copper and oil are cyclical, and "
                            "pooling them produces meaningless correlations."
                        ),
                    }

                spec = engine.root_spec(root)
                if spec is None:
                    known = [str(entry.get("root")) for entry in engine.tracked_roots()]
                    raise refuse(
                        f"root {root!r} is not tracked. Declared roots: {sorted(known)}. "
                        "Add it to universe.yaml rather than substituting a neighbour."
                    )

                state = engine.state(root, as_of=session)
                payload: dict[str, Any] = {
                    "root": root,
                    "spec": to_jsonable(spec),
                    "state": to_jsonable(state),
                    "wrapper": to_jsonable(state.wrapper),
                    "rejects_futures_tracking": state.wrapper.rejects_futures_tracking,
                    "futures_contract_note": (
                        "The futures CONTRACT is prohibited; reading the curve is not. "
                        "Express the view through a physical ETC, producer equity or a "
                        "futures-tracking fund — never the contract itself."
                    ),
                }
                if include_proxies:
                    proxies = engine.proxies_for(root, as_of=session)
                    payload["proxies"] = to_jsonable(proxies)
                    unreliable = [p for p in proxies if not p.reliable]
                    if unreliable:
                        payload["unreliable_proxies"] = [p.symbol for p in unreliable]
                        payload["proxy_note"] = (
                            "a proxy below commodities.proxies.min_r2 must not be "
                            "presented as commodity exposure. A diversified specialty "
                            "chemicals company is not lithium."
                        )
                if include_agri_linkage:
                    payload["agri_linkage"] = to_jsonable(engine.agri_linkage(root, as_of=session))
                return payload

        return envelope(context, as_of=session, data=await call("get_commodity_state", work))
