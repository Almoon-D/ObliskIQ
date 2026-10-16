"""Shadow mode's fills: simulated with the cost model, never sent (MASTER_PLAN §5.12, §5.19).

A shadow fill is priced where a real one would land: the quote's reference price moved against
the order by the cost model's spread and slippage per unit, with the model's commission. The
order fills only when that price is inside its limit; a sell stop-limit first needs the
reference at or below its stop. Shadow, paper and live therefore charge the same costs, because
they call the same functions.

Nothing is invented: a quote without a reference price, a leg the cost model cannot price (an
unknown schedule, tax or friction tier), a buy stop-limit or a currency conversion is not filled,
and the outcome says why.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_FLOOR, Decimal

from ..domain.enums import AssetClass
from ..domain.orders import Fill, OrderSide, OrderTerms, QuoteEvidence
from ..quant.costs import CostBook, LegCost
from ..quant.costs import OrderTerms as CostTerms
from ..util.timeutil import utc_stamp


@dataclass(frozen=True, slots=True)
class ShadowOutcome:
    """A simulated fill, or why there is none; ``cost`` is the leg's priced cost."""

    fill: Fill | None
    cost: LegCost | None
    reason: str


def _none(reason: str, cost: LegCost | None = None) -> ShadowOutcome:
    return ShadowOutcome(fill=None, cost=cost, reason=reason)


class ShadowFiller:
    """Fills shadow orders against a quote with the cost model's frictions."""

    def __init__(self, costs: CostBook) -> None:
        self._costs = costs

    def simulate(
        self,
        terms: OrderTerms,
        quote: QuoteEvidence,
        *,
        coid: str,
        asset_class: AssetClass,
        venue: str,
        currency: str,
        at: datetime,
        multiplier: Decimal | None = None,
        market_cap_usd: Decimal | None = None,
        average_daily_value: Decimal | None = None,
        issuer_country: str | None = None,
        taxable_issuer: bool | None = None,
    ) -> ShadowOutcome:
        if terms.conversion:
            return _none("currency conversions are not simulated")
        reference = quote.reference()
        if reference is None:
            return _none("the quote has no reference price")
        price_policy = terms.price
        if price_policy.kind == "stop_limit":
            if terms.side is not OrderSide.SELL:
                return _none("a buy stop-limit is not simulated")
            if reference > price_policy.stop:
                return _none("the stop is not triggered")
        unit = Decimal(1) if multiplier is None else multiplier
        if terms.quantity is not None:
            quantity = terms.quantity
        else:
            cash = terms.cash_quantity or Decimal(0)
            quantity = (cash / (reference * unit)).to_integral_value(rounding=ROUND_FLOOR)
        if quantity <= 0:
            return _none("the cash quantity buys less than one unit")
        cost = self._costs.leg_cost(
            CostTerms(
                venue=venue,
                asset_class=asset_class,
                currency=currency,
                quantity=quantity,
                price=reference,
            ),
            side="buy" if terms.side is OrderSide.BUY else "sell",
            on=at.date(),
            issuer_country=issuer_country,
            taxable_issuer=taxable_issuer,
            market_cap_usd=market_cap_usd,
            average_daily_value=average_daily_value,
            multiplier=multiplier,
        )
        if cost.total is None or cost.spread_cost is None or cost.slippage_cost is None:
            gaps = ", ".join(gap.value for gap in cost.gaps) or "a component without a price"
            return _none(f"the cost model cannot price this leg ({gaps})", cost)
        friction = (cost.spread_cost + cost.slippage_cost) / (quantity * unit)
        buying = terms.side is OrderSide.BUY
        price = reference + friction if buying else reference - friction
        limit = price_policy.limit
        if price <= 0 or (buying and price > limit) or (not buying and price < limit):
            return _none(f"the simulated price {price} is outside the limit {limit}", cost)
        fill = Fill(
            execution_id=f"shadow-{coid}-{utc_stamp(at)}",
            coid=coid,
            conid=terms.conid,
            side=terms.side,
            quantity=quantity,
            price=price,
            commission=cost.commission.amount,
            executed_at=at,
            simulated=True,
        )
        return ShadowOutcome(fill=fill, cost=cost, reason="filled in shadow")
