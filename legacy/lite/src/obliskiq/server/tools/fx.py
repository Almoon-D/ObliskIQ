"""``get_fx_real_returns`` — the four-way decomposition and the carry test.

Nominal returns from high-inflation, depreciating-currency markets are mirages.
A market up 60% in local terms can be down in euros, and a 35% nominal bond
yield against 35% inflation and 20% annual depreciation is a negative real
return. So every market return is decomposed four ways — local nominal, local
real, base nominal, base real — and **ranking consumes base-real only**. The
engine refuses to construct on a config that names any other basis; local
nominal figures come back marked display-only and cannot drive a decision.

The carry-illusion test is mandatory for any rate or bond idea. Under covered
interest parity the forward discount approximates the rate differential, so a
hedged high-yield bond returns roughly the base-currency rate — which is what
kills most headline emerging-market yields.

No physical hedging is ever recommended. Currency here is a measurement and
ranking discipline, not an execution mandate.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from ...compute.build_grid import GridBuilder
from ...compute.fx_engine import FxEngine
from ...logging_setup import get_logger
from ..context import ServerContext
from ..encode import to_jsonable
from .base import READ_ONLY, call, envelope, refuse

log = get_logger("server.tools.fx")


def register(mcp: FastMCP, context: ServerContext) -> None:
    @mcp.tool(
        name="get_fx_real_returns",
        description=(
            "Four-way return decomposition per market — local nominal, local real, base "
            "nominal, base real — with high-inflation and high-depreciation flags. "
            "Ranking consumes BASE REAL only; local nominal is display-only. Pass "
            "`nominal_yield` with a `currency` to run the mandatory carry-illusion test "
            "on a rate or bond idea: a high nominal yield is not an opportunity unless "
            "the hedged-equivalent or base-real yield is high."
        ),
        annotations=READ_ONLY,
    )
    async def get_fx_real_returns(
        market: str | None = None,
        as_of: str | None = None,
        horizon_days: int | None = None,
        currency: str | None = None,
        nominal_yield: float | None = None,
        local_rate: float | None = None,
        base_rate: float | None = None,
    ) -> dict[str, Any]:
        """The four-way decomposition, and optionally the carry-illusion test.

        Args:
            market: Market key from universe.yaml. Omit for every market, ranked
                on base-real.
            as_of: Session date, YYYY-MM-DD. Defaults to today in UTC.
            horizon_days: Return horizon. Defaults to one calendar year.
            currency: Currency for the carry-illusion test, e.g. "TRY".
            nominal_yield: Headline yield as a decimal (0.35 = 35%). Supplying it
                with `currency` runs the carry-illusion test.
            local_rate: Local policy rate as a decimal. Defaults to the stored one.
            base_rate: Base-currency policy rate as a decimal. Defaults to stored.
        """
        session = context.as_of(as_of)
        if nominal_yield is not None and currency is None:
            raise refuse(
                "the carry-illusion test needs the currency the yield is quoted in: the "
                "hedged equivalent is the base rate plus that currency's differential."
            )

        def work() -> dict[str, Any]:
            with context.store() as store:
                builder = GridBuilder(store, context.config)
                engine = FxEngine(store, context.config, builder.markets)
                horizon = horizon_days or round(
                    context.config.require_float("numerics.bsm.day_count_basis")
                )

                payload: dict[str, Any] = {
                    "horizon_days": horizon,
                    "rank_on": "base_real",
                    "ranking_note": (
                        "local nominal figures below are display-only and may never drive "
                        "a decision. Ranking happens on base real."
                    ),
                    "hedging_note": (
                        "no physical FX hedging is recommended. Currency is a measurement "
                        "and ranking discipline, not an execution mandate: screen and rank "
                        "in base-real terms and carry the exposure knowingly."
                    ),
                }

                if market is not None:
                    if market not in builder.markets:
                        raise refuse(
                            f"market {market!r} is not declared. Declared markets: "
                            f"{sorted(builder.markets)}."
                        )
                    payload["decomposition"] = to_jsonable(
                        engine.decompose(market, as_of=session, horizon_days=horizon)
                    )
                else:
                    results = engine.decompose_all(as_of=session, horizon_days=horizon)
                    ranked = engine.rank(results)
                    payload["decompositions"] = to_jsonable(results[: context.max_rows])
                    payload["ranked_on_base_real"] = to_jsonable(
                        [row.market for row in ranked][: context.max_rows]
                    )
                    payload["flagged"] = [row.market for row in results if row.flagged]
                    payload["flagged_market_rule"] = engine.flagged_market_rule

                if nominal_yield is not None and currency is not None:
                    test = engine.carry_illusion(
                        currency,
                        nominal_yield=nominal_yield,
                        as_of=session,
                        local_rate=local_rate,
                        base_rate=base_rate,
                    )
                    payload["carry_test"] = to_jsonable(test)
                    payload["carry_is_illusion"] = test.is_illusion
                return payload

        return envelope(context, as_of=session, data=await call("get_fx_real_returns", work))
